# osmo_gsm_tester: specifics for running an SRS UE process
#
# Copyright (C) 2020 by sysmocom - s.f.m.c. GmbH
#
# Author: Pau Espin Pedrol <pespin@sysmocom.de>
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
import pprint

from . import log, util, config, template, process, remote
from .run_node import RunNode
from .event_loop import MainLoop
from .ms import MS

def rf_type_valid(rf_type_str):
    return rf_type_str in ('zmq', 'UHD', 'soapy', 'bladeRF')

#reference: srsLTE.git srslte_symbol_sz()
def num_prb2symbol_sz(num_prb):
    if num_prb <= 6:
        return 128
    if num_prb <= 15:
        return 256
    if num_prb <= 25:
        return 384
    if num_prb <= 50:
        return 768
    if num_prb <= 75:
        return 1024
    if num_prb <= 110:
        return 1536
    raise log.Error('invalid num_prb %r', num_prb)

def num_prb2base_srate(num_prb):
    return num_prb2symbol_sz(num_prb) * 15 * 1000

class srsUE(MS):

    REMOTE_DIR = '/osmo-gsm-tester-srsue'
    BINFILE = 'srsue'
    CFGFILE = 'srsue.conf'
    PCAPFILE = 'srsue.pcap'
    LOGFILE = 'srsue.log'
    METRICSFILE = 'srsue_metrics.csv'

    def __init__(self, suite_run, conf):
        self._addr = conf.get('addr', None)
        if self._addr is None:
            raise log.Error('addr not set')
        super().__init__('srsue_%s' % self._addr, conf)
        self.enb = None
        self.run_dir = None
        self.config_file = None
        self.log_file = None
        self.pcap_file = None
        self.metrics_file = None
        self.process = None
        self.rem_host = None
        self.remote_config_file = None
        self.remote_log_file = None
        self.remote_pcap_file = None
        self.remote_metrics_file = None
        self.enable_pcap = False
        self.suite_run = suite_run
        self.remote_user = conf.get('remote_user', None)
        if not rf_type_valid(conf.get('rf_dev_type', None)):
            raise log.Error('Invalid rf_dev_type=%s' % conf.get('rf_dev_type', None))

    def cleanup(self):
        if self.process is None:
            return
        if self.setup_runs_locally():
            return
        # When using zmq, srsUE is known to hang for a few seconds before
        # exiting (3 seconds after alarm() watchdog kicks in). We hence need to
        # wait to make sure the remote process terminated and the file was
        # flushed, since cleanup() triggered means only the local ssh client was killed.
        if self._conf and self._conf.get('rf_dev_type', '') == 'zmq':
            MainLoop.sleep(self, 3)
        # copy back files (may not exist, for instance if there was an early error of process):
        try:
            self.rem_host.scpfrom('scp-back-log', self.remote_log_file, self.log_file)
        except Exception as e:
            self.log(repr(e))
        if self.enable_pcap:
            try:
                self.rem_host.scpfrom('scp-back-pcap', self.remote_pcap_file, self.pcap_file)
            except Exception as e:
                self.log(repr(e))

    def setup_runs_locally(self):
        return self.remote_user is None

    def netns(self):
        return "srsue1"

    def stop(self):
        self.suite_run.stop_process(self.process)

    def connect(self, enb):
        self.log('Starting srsue')
        self.enb = enb
        self.run_dir = util.Dir(self.suite_run.get_test_run_dir().new_dir(self.name()))
        self.configure()
        if self.setup_runs_locally():
            self.start_locally()
        else:
            self.start_remotely()

        # send t+Enter to enable console trace
        self.dbg('Enabling console trace')
        self.process.stdin_write('t\n')

    def start_remotely(self):
        self.inst = util.Dir(os.path.abspath(self.suite_run.trial.get_inst('srslte')))
        lib = self.inst.child('lib')
        if not os.path.isdir(lib):
            raise log.Error('No lib/ in', self.inst)
        if not self.inst.isfile('bin', srsUE.BINFILE):
            raise log.Error('No %s binary in' % srsUE.BINFILE, self.inst)

        self.rem_host = remote.RemoteHost(self.run_dir, self.remote_user, self._addr)
        remote_prefix_dir = util.Dir(srsUE.REMOTE_DIR)
        remote_inst = util.Dir(remote_prefix_dir.child(os.path.basename(str(self.inst))))
        remote_run_dir = util.Dir(remote_prefix_dir.child(srsUE.BINFILE))
        self.remote_config_file = remote_run_dir.child(srsUE.CFGFILE)
        self.remote_log_file = remote_run_dir.child(srsUE.LOGFILE)
        self.remote_pcap_file = remote_run_dir.child(srsUE.PCAPFILE)
        self.remote_metrics_file = remote_run_dir.child(srsUE.METRICSFILE)

        self.rem_host.recreate_remote_dir(remote_inst)
        self.rem_host.scp('scp-inst-to-remote', str(self.inst), remote_prefix_dir)
        self.rem_host.recreate_remote_dir(remote_run_dir)
        self.rem_host.scp('scp-cfg-to-remote', self.config_file, self.remote_config_file)

        remote_lib = remote_inst.child('lib')
        remote_binary = remote_inst.child('bin', srsUE.BINFILE)
        # setting capabilities will later disable use of LD_LIBRARY_PATH from ELF loader -> modify RPATH instead.
        self.log('Setting RPATH for srsue')
        # srsue binary needs patchelf >= 0.9+52 to avoid failing during patch. OS#4389, patchelf-GH#192.
        self.rem_host.set_remote_env({'PATCHELF_BIN': '/opt/bin/patchelf-v0.10' })
        self.rem_host.change_elf_rpath(remote_binary, remote_lib)

        # srsue requires CAP_SYS_ADMIN to cjump to net network namespace: netns(CLONE_NEWNET):
        # srsue requires CAP_NET_ADMIN to create tunnel devices: ioctl(TUNSETIFF):
        self.log('Applying CAP_SYS_ADMIN+CAP_NET_ADMIN capability to srsue')
        self.rem_host.setcap_netsys_admin(remote_binary)

        self.log('Creating netns %s' % self.netns())
        self.rem_host.create_netns(self.netns())

        #'strace', '-ff',
        args = (remote_binary, self.remote_config_file,
                '--gw.netns=' + self.netns(),
                '--log.filename=' + self.remote_log_file,
                '--pcap.filename=' + self.remote_pcap_file,
                '--general.metrics_csv_filename=' + self.remote_metrics_file)

        self.process = self.rem_host.RemoteProcess(srsUE.BINFILE, args)
        #self.process = self.rem_host.RemoteProcessFixIgnoreSIGHUP(srsUE.BINFILE, remote_run_dir, args, remote_lib)
        self.suite_run.remember_to_stop(self.process)
        self.process.launch()

    def start_locally(self):
        inst = util.Dir(os.path.abspath(self.suite_run.trial.get_inst('srslte')))

        binary = inst.child('bin', BINFILE)
        if not os.path.isfile(binary):
            raise log.Error('Binary missing:', binary)
        lib = inst.child('lib')
        if not os.path.isdir(lib):
            raise log.Error('No lib/ in', inst)

        env = {}

        # setting capabilities will later disable use of LD_LIBRARY_PATH from ELF loader -> modify RPATH instead.
        self.log('Setting RPATH for srsue')
        util.change_elf_rpath(binary, util.prepend_library_path(lib), self.run_dir.new_dir('patchelf'))

        # srsue requires CAP_SYS_ADMIN to cjump to net network namespace: netns(CLONE_NEWNET):
        # srsue requires CAP_NET_ADMIN to create tunnel devices: ioctl(TUNSETIFF):
        self.log('Applying CAP_SYS_ADMIN+CAP_NET_ADMIN capability to srsue')
        util.setcap_netsys_admin(binary, self.run_dir.new_dir('setcap_netsys_admin'))

        self.log('Creating netns %s' % self.netns())
        util.create_netns(self.netns(), self.run_dir.new_dir('create_netns'))

        args = (binary, os.path.abspath(self.config_file),
                '--gw.netns=' + self.netns(),
                '--log.filename=' + self.log_file,
                '--pcap.filename=' + self.pcap_file,
                '--general.metrics_csv_filename=' + self.metrics_file)

        self.dbg(run_dir=self.run_dir, binary=binary, env=env)
        self.process = process.Process(self.name(), self.run_dir, args, env=env)
        self.suite_run.remember_to_stop(self.process)
        self.process.launch()

    def configure(self):
        self.config_file = self.run_dir.child(srsUE.CFGFILE)
        self.log_file = self.run_dir.child(srsUE.LOGFILE)
        self.pcap_file = self.run_dir.child(srsUE.PCAPFILE)
        self.metrics_file = self.run_dir.child(srsUE.METRICSFILE)
        self.dbg(config_file=self.config_file)

        values = dict(ue=config.get_defaults('srsue'))
        config.overlay(values, dict(ue=self.suite_run.config().get('modem', {})))
        config.overlay(values, dict(ue=self._conf))
        config.overlay(values, dict(ue=dict(num_antennas = self.enb.num_ports())))

        # Convert parsed boolean string to Python boolean:
        self.enable_pcap = util.str2bool(values['ue'].get('enable_pcap', 'false'))
        config.overlay(values, dict(ue={'enable_pcap': self.enable_pcap}))

        # We need to set some specific variables programatically here to match IP addresses:
        if self._conf.get('rf_dev_type') == 'zmq':
            base_srate = num_prb2base_srate(self.enb.num_prb())
            config.overlay(values, dict(ue=dict(rf_dev_args = 'tx_port=tcp://' + self.addr() + ':2001' \
                                                            + ',tx_port2=tcp://' + self.addr() + ':2003' \
                                                            + ',rx_port=tcp://' + self.enb.addr() + ':2000' \
                                                            + ',rx_port2=tcp://' + self.enb.addr() + ':2002' \
                                                            + ',tx_freq=2510e6,rx_freq=2630e6,tx_freq2=2530e6,rx_freq2=2650e6' \
                                                            + ',id=ue,base_srate='+ str(base_srate)
                                                )))

        self.dbg('SRSUE CONFIG:\n' + pprint.pformat(values))

        with open(self.config_file, 'w') as f:
            r = template.render(srsUE.CFGFILE, values)
            self.dbg(r)
            f.write(r)

    def is_connected(self, mcc_mnc=None):
        return 'Network attach successful.' in (self.process.get_stdout() or '')

    def is_attached(self):
        return self.is_connected()

    def running(self):
        return not self.process.terminated()

    def addr(self):
        return self._addr

    def run_node(self):
        return RunNode(RunNode.T_REM_SSH, self._addr, self.remote_user, self._addr)

    def run_netns_wait(self, name, popen_args):
        if self.setup_runs_locally():
            proc = process.NetNSProcess(name, self.run_dir.new_dir(name), self.netns(), popen_args, env={})
        else:
            proc = self.rem_host.RemoteNetNSProcess(name, self.netns(), popen_args, env={})
        proc.launch_sync()
        return proc

    def verify_metric(self, value, operation='avg', metric='dl_brate', criterion='gt'):
        # file is not properly flushed until the process has stopped.
        if self.running():
            self.stop()
            # metrics file is not flushed immediatelly by the OS during process
            # tear down, we need to wait some extra time:
            MainLoop.sleep(self, 2)
            if not self.setup_runs_locally():
                try:
                    self.rem_host.scpfrom('scp-back-metrics', self.remote_metrics_file, self.metrics_file)
                except Exception as e:
                    self.err('Failed copying back metrics file from remote host')
                    raise e
        metrics = srsUEMetrics(self.metrics_file)
        return metrics.verify(value, operation, metric, criterion)

import numpy

class srsUEMetrics(log.Origin):

    VALID_OPERATIONS = ['avg', 'sum']
    VALID_CRITERION = ['eq','gt','lt']
    CRITERION_TO_SYM = { 'eq' : '==', 'gt' : '>', 'lt' : '<' }
    CRYTERION_TO_SYM_OPPOSITE = { 'eq' : '!=', 'gt' : '<=', 'lt' : '>=' }


    def __init__(self, metrics_file):
        super().__init__(log.C_RUN, 'srsue_metrics')
        self.raw_data = None
        self.metrics_file = metrics_file
        # read CSV, guessing data type with first row being the legend
        try:
            self.raw_data = numpy.genfromtxt(self.metrics_file, names=True, delimiter=';', dtype=None)
        except (ValueError, IndexError, IOError) as error:
            self.err("Error parsing metrics CSV file %s" % self.metrics_file)
            raise error

    def verify(self, value, operation='avg', metric='dl_brate', criterion='gt'):
        if operation not in self.VALID_OPERATIONS:
            raise log.Error('Unknown operation %s not in %r' % (operation, self.VALID_OPERATIONS))
        if criterion not in self.VALID_CRITERION:
            raise log.Error('Unknown operation %s not in %r' % (operation, self.VALID_CRITERION))
        # check if given metric exists in data
        try:
            sel_data = self.raw_data[metric]
        except ValueError as err:
            print('metric %s not available' % metric)
            raise err

        if operation == 'avg':
            result = numpy.average(sel_data)
        elif operation == 'sum':
            result = numpy.sum(sel_data)
        self.dbg(result=result, value=value)

        success = False
        if criterion == 'eq' and result == value or \
           criterion == 'gt' and result > value or \
           criterion == 'lt' and result < value:
            success = True

        # Convert bitrate in Mbit/s:
        if metric.find('brate') > 0:
            result /= 1e6
            value /= 1e6
            mbit_str = ' Mbit/s'
        else:
            mbit_str = ''

        if not success:
            result_msg = "{:.2f}{} {} {:.2f}{}".format(result, mbit_str, self.CRYTERION_TO_SYM_OPPOSITE[criterion], value, mbit_str)
            raise log.Error(result_msg)
        result_msg = "{:.2f}{} {} {:.2f}{}".format(result, mbit_str, self.CRITERION_TO_SYM[criterion], value, mbit_str)
        # TODO: overwrite test system-out with this text.
        return result_msg

# vim: expandtab tabstop=4 shiftwidth=4
