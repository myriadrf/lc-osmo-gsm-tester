# osmo_gsm_tester: test suite
#
# Copyright (C) 2016-2017 by sysmocom - s.f.m.c. GmbH
#
# Author: Neels Hofmeyr <neels@hofmeyr.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import time
import copy
import traceback
import pprint
from . import config, log, template, util, resource, schema, ofono_client, event_loop
from . import osmo_nitb
from . import osmo_hlr, osmo_mgcpgw, osmo_msc, osmo_bsc
from . import test

class Timeout(Exception):
    pass

class SuiteDefinition(log.Origin):
    '''A test suite reserves resources for a number of tests.
       Each test requires a specific number of modems, BTSs etc., which are
       reserved beforehand by a test suite. This way several test suites can be
       scheduled dynamically without resource conflicts arising halfway through
       the tests.'''

    CONF_FILENAME = 'suite.conf'

    CONF_SCHEMA = util.dict_add(
        {
            'defaults.timeout': schema.STR,
        },
        dict([('resources.%s' % k, t) for k,t in resource.WANT_SCHEMA.items()])
        )


    def __init__(self, suite_dir):
        self.suite_dir = suite_dir
        super().__init__(log.C_CNF, os.path.basename(self.suite_dir))
        self.read_conf()

    def read_conf(self):
        self.dbg('reading %s' % SuiteDefinition.CONF_FILENAME)
        if not os.path.isdir(self.suite_dir):
            raise RuntimeError('No such directory: %r' % self.suite_dir)
        self.conf = config.read(os.path.join(self.suite_dir,
                                             SuiteDefinition.CONF_FILENAME),
                                SuiteDefinition.CONF_SCHEMA)
        self.load_test_basenames()

    def load_test_basenames(self):
        self.test_basenames = []
        for basename in sorted(os.listdir(self.suite_dir)):
            if not basename.endswith('.py'):
                continue
            self.test_basenames.append(basename)


class Test(log.Origin):
    UNKNOWN = 'UNKNOWN'
    SKIP = 'skip'
    PASS = 'pass'
    FAIL = 'FAIL'

    def __init__(self, suite_run, test_basename):
        self.basename = test_basename
        super().__init__(log.C_TST, self.basename)
        self.suite_run = suite_run
        self.path = os.path.join(self.suite_run.definition.suite_dir, self.basename)
        self.status = Test.UNKNOWN
        self.start_timestamp = 0
        self.duration = 0
        self.fail_type = None
        self.fail_message = None

    def run(self):
        try:
            log.large_separator(self.suite_run.trial.name(), self.suite_run.name(), self.name(), sublevel=3)
            self.status = Test.UNKNOWN
            self.start_timestamp = time.time()
            test.setup(self.suite_run, self, ofono_client, sys.modules[__name__], event_loop)
            with self.redirect_stdout():
                util.run_python_file('%s.%s' % (self.suite_run.definition.name(), self.basename),
                                     self.path)
            if self.status == Test.UNKNOWN:
                 self.set_pass()
        except Exception as e:
            if hasattr(e, 'msg'):
                msg = e.msg
            else:
                msg = str(e)
            if isinstance(e, AssertionError):
                # AssertionError lacks further information on what was
                # asserted. Find the line where the code asserted:
                msg += log.get_src_from_tb(sys.exc_info()[2])
            # add source file information to failure report
            if hasattr(e, 'origins'):
                msg += ' [%s]' % e.origins
            tb_str = traceback.format_exc()
            if isinstance(e, resource.NoResourceExn):
                tb_str += self.suite_run.resource_status_str()
            self.set_fail(type(e).__name__, msg, tb_str)
        except BaseException as e:
            # when the program is aborted by a signal (like Ctrl-C), escalate to abort all.
            self.err('TEST RUN ABORTED: %s' % type(e).__name__)
            raise

    def name(self):
        l = log.get_line_for_src(self.path)
        if l is not None:
            return '%s:%s' % (self._name, l)
        return super().name()

    def set_fail(self, fail_type, fail_message, tb_str=None):
        self.status = Test.FAIL
        self.duration = time.time() - self.start_timestamp
        self.fail_type = fail_type
        self.fail_message = fail_message

        if tb_str is None:
            # populate an exception-less call to set_fail() with traceback info
            tb_str = ''.join(traceback.format_stack()[:-1])

        self.fail_tb = tb_str
        self.err('%s: %s' % (self.fail_type, self.fail_message))
        if self.fail_tb:
            self.log(self.fail_tb, _level=log.L_TRACEBACK)
        self.log('Test FAILED (%.1f sec)' % self.duration)

    def set_pass(self):
        self.status = Test.PASS
        self.duration = time.time() - self.start_timestamp
        self.log('Test passed (%.1f sec)' % self.duration)

    def set_skip(self):
        self.status = Test.SKIP
        self.duration = 0

class SuiteRun(log.Origin):
    UNKNOWN = 'UNKNOWN'
    PASS = 'PASS'
    FAIL = 'FAIL'

    trial = None
    status = None
    start_timestamp = None
    duration = None
    resources_pool = None
    reserved_resources = None
    objects_to_clean_up = None
    _resource_requirements = None
    _config = None
    _processes = None

    def __init__(self, trial, suite_scenario_str, suite_definition, scenarios=[]):
        super().__init__(log.C_TST, suite_scenario_str)
        self.trial = trial
        self.definition = suite_definition
        self.scenarios = scenarios
        self.resources_pool = resource.ResourcesPool()
        self.status = SuiteRun.UNKNOWN
        self.load_tests()

    def load_tests(self):
        self.tests = []
        for test_basename in self.definition.test_basenames:
            self.tests.append(Test(self, test_basename))

    def register_for_cleanup(self, *obj):
        assert all([hasattr(o, 'cleanup') for o in obj])
        self.objects_to_clean_up = self.objects_to_clean_up or []
        self.objects_to_clean_up.extend(obj)

    def objects_cleanup(self):
        while self.objects_to_clean_up:
            obj = self.objects_to_clean_up.pop()
            obj.cleanup()

    def mark_start(self):
        self.start_timestamp = time.time()
        self.duration = 0
        self.status = SuiteRun.UNKNOWN

    def combined(self, conf_name):
        log.dbg(combining=conf_name)
        log_ctx = 'combining_scenarios=%r' % conf_name
        combination = copy.deepcopy(self.definition.conf.get(conf_name) or {})
        self.dbg(definition_conf=combination)
        for scenario in self.scenarios:
            log_ctx = '{combining_scenarios=%r, %s}' % (conf_name, scenario.name())
            c = scenario.get(conf_name)
            self.dbg(scenario=scenario.name(), conf=c)
            if c is None:
                continue
            config.combine(combination, c)
        return combination

    def resource_requirements(self):
        if self._resource_requirements is None:
            self._resource_requirements = self.combined('resources')
        return self._resource_requirements

    def config(self):
        if self._config is None:
            self._config = self.combined('config')
        return self._config

    def reserve_resources(self):
        if self.reserved_resources:
            raise RuntimeError('Attempt to reserve resources twice for a SuiteRun')
        self.log('reserving resources in', self.resources_pool.state_dir, '...')
        self.reserved_resources = self.resources_pool.reserve(self, self.resource_requirements())

    def run_tests(self, names=None):
        try:
            log.large_separator(self.trial.name(), self.name(), sublevel=2)
            self.mark_start()
            event_loop.register_poll_func(self.poll)
            if not self.reserved_resources:
                self.reserve_resources()
            for test in self.tests:
                if names and not test.name() in names:
                    test.set_skip()
                    continue
                test.run()
        except Exception:
            self.log_exn()
        except BaseException as e:
            # when the program is aborted by a signal (like Ctrl-C), escalate to abort all.
            self.err('SUITE RUN ABORTED: %s' % type(e).__name__)
            raise
        finally:
            # if sys.exit() called from signal handler (e.g. SIGINT), SystemExit
            # base exception is raised. Make sure to stop processes in this
            # finally section. Resources are automatically freed with 'atexit'.
            self.stop_processes()
            self.objects_cleanup()
            self.free_resources()
            event_loop.unregister_poll_func(self.poll)
            self.duration = time.time() - self.start_timestamp

            passed, skipped, failed = self.count_test_results()
            # if no tests ran, count it as failure
            if passed and not failed:
                self.status = SuiteRun.PASS
            else:
                self.status = SuiteRun.FAIL

            log.large_separator(self.trial.name(), self.name(), self.status, sublevel=2, space_above=False)

    def passed(self):
        return self.status == SuiteRun.PASS

    def count_test_results(self):
        passed = 0
        skipped = 0
        failed = 0
        for test in self.tests:
            if test.status == Test.PASS:
                passed += 1
            elif test.status == Test.FAIL:
                failed += 1
            else:
                skipped += 1
        return (passed, skipped, failed)

    def remember_to_stop(self, process):
        if self._processes is None:
            self._processes = []
        self._processes.insert(0, process)

    def stop_processes(self):
        if not self._processes:
            return
        for process in self._processes:
            process.terminate()

    def free_resources(self):
        if self.reserved_resources is None:
            return
        self.reserved_resources.free()

    def ip_address(self, specifics=None):
        return self.reserved_resources.get(resource.R_IP_ADDRESS, specifics=specifics)

    def nitb(self, ip_address=None):
        if ip_address is None:
            ip_address = self.ip_address()
        return osmo_nitb.OsmoNitb(self, ip_address)

    def hlr(self, ip_address=None):
        if ip_address is None:
            ip_address = self.ip_address()
        return osmo_hlr.OsmoHlr(self, ip_address)

    def mgcpgw(self, ip_address=None, bts_ip=None):
        if ip_address is None:
            ip_address = self.ip_address()
        return osmo_mgcpgw.OsmoMgcpgw(self, ip_address, bts_ip)

    def msc(self, hlr, mgcpgw, ip_address=None):
        if ip_address is None:
            ip_address = self.ip_address()
        return osmo_msc.OsmoMsc(self, hlr, mgcpgw, ip_address)

    def bsc(self, msc, ip_address=None):
        if ip_address is None:
            ip_address = self.ip_address()
        return osmo_bsc.OsmoBsc(self, msc, ip_address)

    def bts(self, specifics=None):
        return bts_obj(self, self.reserved_resources.get(resource.R_BTS, specifics=specifics))

    def modem(self, specifics=None):
        conf = self.reserved_resources.get(resource.R_MODEM, specifics=specifics)
        self.dbg('create Modem object', conf=conf)
        modem = ofono_client.Modem(conf)
        self.register_for_cleanup(modem)
        return modem

    def modems(self, count):
        l = []
        for i in range(count):
            l.append(self.modem())
        return l

    def msisdn(self):
        msisdn = self.resources_pool.next_msisdn(self.origin)
        self.log('using MSISDN', msisdn)
        return msisdn

    def poll(self):
        if self._processes:
            for process in self._processes:
                if process.terminated():
                    process.log_stdout_tail()
                    process.log_stderr_tail()
                    log_ctx = process
                    raise log.Error('Process ended prematurely')

    def prompt(self, *msgs, **msg_details):
        'ask for user interaction. Do not use in tests that should run automatically!'
        if msg_details:
            msgs = list(msgs)
            msgs.append('{%s}' %
                        (', '.join(['%s=%r' % (k,v)
                                    for k,v in sorted(msg_details.items())])))
        msg = ' '.join(msgs) or 'Hit Enter to continue'
        self.log('prompt:', msg)
        sys.__stdout__.write('\n\n--- PROMPT ---\n')
        sys.__stdout__.write(msg)
        sys.__stdout__.write('\n')
        sys.__stdout__.flush()
        entered = util.input_polling('> ', self.poll)
        self.log('prompt entered:', repr(entered))
        return entered

    def resource_status_str(self):
        return '\n'.join(('',
            'SUITE RUN: %s' % self.origin_id(),
            'ASKED FOR:', pprint.pformat(self._resource_requirements),
            'RESERVED COUNT:', pprint.pformat(self.reserved_resources.counts()),
            'RESOURCES STATE:', repr(self.reserved_resources)))

loaded_suite_definitions = {}

def load(suite_name):
    global loaded_suite_definitions

    suite = loaded_suite_definitions.get(suite_name)
    if suite is not None:
        return suite

    suites_dir = config.get_suites_dir()
    suite_dir = suites_dir.child(suite_name)
    if not suites_dir.exists(suite_name):
        raise RuntimeError('Suite not found: %r in %r' % (suite_name, suites_dir))
    if not suites_dir.isdir(suite_name):
        raise RuntimeError('Suite name found, but not a directory: %r' % (suite_dir))

    suite_def = SuiteDefinition(suite_dir)
    loaded_suite_definitions[suite_name] = suite_def
    return suite_def

def parse_suite_scenario_str(suite_scenario_str):
    tokens = suite_scenario_str.split(':')
    if len(tokens) > 2:
        raise RuntimeError('invalid combination string: %r' % suite_scenario_str)

    suite_name = tokens[0]
    if len(tokens) <= 1:
        scenario_names = []
    else:
        scenario_names = tokens[1].split('+')

    return suite_name, scenario_names

def load_suite_scenario_str(suite_scenario_str):
    suite_name, scenario_names = parse_suite_scenario_str(suite_scenario_str)
    suite = load(suite_name)
    scenarios = [config.get_scenario(scenario_name) for scenario_name in scenario_names]
    return (suite_scenario_str, suite, scenarios)

def bts_obj(suite_run, conf):
    bts_type = conf.get('type')
    log.dbg(None, None, 'create BTS object', type=bts_type)
    bts_class = resource.KNOWN_BTS_TYPES.get(bts_type)
    if bts_class is None:
        raise RuntimeError('No such BTS type is defined: %r' % bts_type)
    return bts_class(suite_run, conf)

# vim: expandtab tabstop=4 shiftwidth=4
