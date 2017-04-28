# osmo_gsm_tester: specifics for running a sysmoBTS
#
# Copyright (C) 2016-2017 by sysmocom - s.f.m.c. GmbH
#
# Author: Neels Hofmeyr <neels@hofmeyr.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
from . import log, config, util, template, process

class SysmoBts(log.Origin):
    suite_run = None
    nitb = None
    run_dir = None
    inst = None
    remote_addr = None
    remote_inst = None
    remote_env = None
    remote_dir = None

    REMOTE_DIR = '/osmo-gsm-tester'
    BTS_SYSMO_BIN = 'osmo-bts-sysmo'
    BTS_SYSMO_CFG = 'osmo-bts-sysmo.cfg'

    def __init__(self, suite_run, conf):
        self.suite_run = suite_run
        self.conf = conf
        self.set_name('osmo-bts-sysmo')
        self.set_log_category(log.C_RUN)
        self.remote_env = {}
        self.remote_user = 'root'

    def start(self):
        with self:
            if self.nitb is None:
                raise RuntimeError('BTS needs to be added to a NITB before it can be started')
            self.log('Starting sysmoBTS to connect to', self.nitb)
            self.run_dir = util.Dir(self.suite_run.trial.get_run_dir().new_dir(self.name()))
            self.configure()

            self.inst = util.Dir(os.path.abspath(self.suite_run.trial.get_inst(SysmoBts.BTS_SYSMO_BIN)))
            lib = self.inst.child('lib')
            if not os.path.isdir(lib):
                self.raise_exn('No lib/ in', self.inst)
            if not self.inst.isfile('bin', SysmoBts.BTS_SYSMO_BIN):
                self.raise_exn('No osmo-bts-sysmo binary in', self.inst)

            self.remote_dir = util.Dir(SysmoBts.REMOTE_DIR)
            self.remote_inst = util.Dir(self.remote_dir.child(os.path.basename(str(self.inst))))

            self.run_remote('rm-remote-dir', ('test', '!', '-d', SysmoBts.REMOTE_DIR, '||', 'rm', '-rf', SysmoBts.REMOTE_DIR))
            self.run_remote('mk-remote-dir', ('mkdir', '-p', SysmoBts.REMOTE_DIR))
            self.run_local('scp-inst-to-sysmobts',
                ('scp', '-r', str(self.inst), '%s@%s:%s' % (self.remote_user, self.remote_addr, str(self.remote_inst))))

            remote_run_dir = self.remote_dir.child(SysmoBts.BTS_SYSMO_BIN)
            self.run_remote('mk-remote-run-dir', ('mkdir', '-p', remote_run_dir))

            remote_config_file = self.remote_dir.child(SysmoBts.BTS_SYSMO_CFG)
            self.run_local('scp-cfg-to-sysmobts',
                ('scp', '-r', self.config_file, '%s@%s:%s' % (self.remote_user, self.remote_addr, remote_config_file)))

            self.run_local('reload-dsp-firmware', ('ssh', self.remote_addr, '/bin/sh', '-c', '"cat /lib/firmware/sysmobts-v?.bit > /dev/fpgadl_par0 ; cat /lib/firmware/sysmobts-v?.out > /dev/dspdl_dm644x_0"'))

            remote_lib = self.remote_inst.child('lib')
            remote_binary = self.remote_inst.child('bin', 'osmo-bts-sysmo')
            self.launch_remote('osmo-bts-sysmo',
                ('LD_LIBRARY_PATH=%s' % remote_lib,
                 remote_binary, '-c', remote_config_file, '-r', '1'),
                remote_cwd=remote_run_dir)

    def _process_remote(self, name, popen_args, remote_cwd=None):
        run_dir = self.run_dir.new_dir(name)
        return process.RemoteProcess(name, run_dir, self.remote_user, self.remote_addr, remote_cwd,
                                     popen_args)

    def run_remote(self, name, popen_args, remote_cwd=None):
        proc = self._process_remote(name, popen_args, remote_cwd)
        proc.launch()
        proc.wait()
        if proc.result != 0:
            proc.raise_exn('Exited in error')

    def launch_remote(self, name, popen_args, remote_cwd=None):
        proc = self._process_remote(name, popen_args, remote_cwd)
        self.suite_run.remember_to_stop(proc)
        proc.launch()

    def run_local(self, name, popen_args):
        run_dir = self.run_dir.new_dir(name)
        proc = process.Process(name, run_dir, popen_args)
        proc.launch()
        proc.wait()
        if proc.result != 0:
            proc.raise_exn('Exited in error')

    def configure(self):
        if self.nitb is None:
            raise RuntimeError('BTS needs to be added to a NITB before it can be configured')

        self.remote_addr = self.conf.get('addr')

        self.config_file = self.run_dir.new_file(SysmoBts.BTS_SYSMO_CFG)
        self.dbg(config_file=self.config_file)

        values = { 'osmo_bts_sysmo': config.get_defaults('osmo_bts_sysmo') }
        config.overlay(values, self.suite_run.config())
        config.overlay(values, { 'osmo_bts_sysmo': { 'oml_remote_ip': self.nitb.addr() } })
        config.overlay(values, { 'osmo_bts_sysmo': self.conf })
        self.dbg(conf=values)

        with open(self.config_file, 'w') as f:
            r = template.render(SysmoBts.BTS_SYSMO_CFG, values)
            self.dbg(r)
            f.write(r)

    def conf_for_nitb(self):
        values = config.get_defaults('nitb_bts')
        config.overlay(values, config.get_defaults('osmo_bts_sysmo'))
        config.overlay(values, self.conf)
        self.dbg(conf=values)
        return values

    def set_nitb(self, nitb):
        self.nitb = nitb

# vim: expandtab tabstop=4 shiftwidth=4
