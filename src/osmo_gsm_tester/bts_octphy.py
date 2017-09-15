# osmo_gsm_tester: specifics for running an osmo-bts-octphy
#
# Copyright (C) 2016-2017 by sysmocom - s.f.m.c. GmbH
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
import tempfile
from . import log, config, util, template, process, event_loop

class OsmoBtsOctphy(log.Origin):
    suite_run = None
    bsc = None
    run_dir = None
    inst = None
    env = None
    pcu_sk_tmp_dir = None
    values = None

    BIN_BTS_OCTPHY = 'osmo-bts-octphy'

    CONF_BTS_OCTPHY = 'osmo-bts-octphy.cfg'

    def __init__(self, suite_run, conf):
        super().__init__(log.C_RUN, OsmoBtsOctphy.BIN_BTS_OCTPHY)
        self.suite_run = suite_run
        self.conf = conf
        self.env = {}
        self.values = {}
        self.pcu_sk_tmp_dir = tempfile.mkdtemp('', 'ogtpcusk')
        if len(self.pcu_socket_path().encode()) > 107:
            raise log.Error('Path for pcu socket is longer than max allowed len for unix socket path (107):', self.pcu_socket_path())

    def cleanup(self):
        if self.pcu_sk_tmp_dir:
            try:
                os.remove(self.pcu_socket_path())
            except OSError:
                pass
            os.rmdir(self.pcu_sk_tmp_dir)

    def pcu_socket_path(self):
        return os.path.join(self.pcu_sk_tmp_dir, 'pcu_bts')

    def remote_addr(self):
        return self.conf.get('addr')

    def start(self):
        if self.bsc is None:
            raise RuntimeError('BTS needs to be added to a BSC or NITB before it can be started')
        self.suite_run.poll()

        self.log('Starting to connect to', self.bsc)
        self.run_dir = util.Dir(self.suite_run.get_test_run_dir().new_dir(self.name()))
        self.configure()

        self.inst = util.Dir(os.path.abspath(self.suite_run.trial.get_inst('osmo-bts')))
        btsoct_path = self.inst.child('bin', OsmoBtsOctphy.BIN_BTS_OCTPHY)
        lib = self.inst.child('lib')
        if not os.path.isdir(lib):
            raise RuntimeError('No lib/ in %r' % self.inst)

        # setting capabilities will later disable use of LD_LIBRARY_PATH from ELF loader -> modify RPATH instead.
        self.log('Setting RPATH for', OsmoBtsOctphy.BIN_BTS_OCTPHY)
        util.change_elf_rpath(btsoct_path, util.prepend_library_path(lib), self.run_dir.new_dir('patchelf'))
        # osmo-bty-octphy requires CAP_NET_RAW to open AF_PACKET socket:
        self.log('Applying CAP_NET_RAW capability to', OsmoBtsOctphy.BIN_BTS_OCTPHY)
        util.setcap_net_raw(btsoct_path, self.run_dir.new_dir('setcap_net_raw'))

        self.launch_process(OsmoBtsOctphy.BIN_BTS_OCTPHY, '-r', '1',
                            '-c', os.path.abspath(self.config_file),
                            '-i', self.bsc.addr(), '-t', str(self.num_trx()))
        #self.launch_process(OsmoBtsOctphy.BIN_PCU, '-r', '1')
        self.suite_run.poll()

    def launch_process(self, binary_name, *args):
        binary = os.path.abspath(self.inst.child('bin', binary_name))
        run_dir = self.run_dir.new_dir(binary_name)
        if not os.path.isfile(binary):
            raise RuntimeError('Binary missing: %r' % binary)
        proc = process.Process(binary_name, run_dir,
                               (binary,) + args,
                               env=self.env)
        self.suite_run.remember_to_stop(proc)
        proc.launch()
        return proc

    def num_trx(self):
        return len(self.values['osmo_bts_octphy'].get('trx_list', []))

    def allocate_phy_instances(self, c):
        '''
        Generate match trx Z <-> phy X inst Y to use in vty config

        We create a new phy for each trx found with a new hwaddr. If hwaddr is
        already there, increase num_instances and give last instance index to
        the current trx.
        '''
        phy_list = []
        for trx in c.get('trx_list', []):
            hwaddr = trx.get('hw_addr', None)
            netdev = trx.get('net_device', None)
            if hwaddr is None:
                raise log.Error('Expected hw-addr value not found!')
            found = False
            phy_idx = 0
            for phy in phy_list:
                if phy['hw_addr'] == hwaddr:
                    phy['num_instances'] += 1
                    found = True
                    break
                phy_idx += 1
            if not found:
                phy_list.append({'hw_addr': hwaddr, 'net_device': netdev, 'num_instances': 1})
            trx['phy_idx'] = phy_idx
            trx['instance_idx'] = phy_list[phy_idx]['num_instances'] - 1
        c['phy_list'] = phy_list

    def configure(self):
        if self.bsc is None:
            raise RuntimeError('BTS needs to be added to a BSC or NITB before it can be configured')
        self.config_file = self.run_dir.new_file(OsmoBtsOctphy.CONF_BTS_OCTPHY)
        self.dbg(config_file=self.config_file)

        values = dict(osmo_bts_octphy=config.get_defaults('osmo_bts_octphy'))
        config.overlay(values, self.suite_run.config())
        config.overlay(values, {
                        'osmo_bts_octphy': {
                            'oml_remote_ip': self.bsc.addr(),
                            'pcu_socket_path': self.pcu_socket_path(),
                        }
        })
        config.overlay(values, { 'osmo_bts_octphy': self.conf })

        self.allocate_phy_instances(values['osmo_bts_octphy'])

        self.dbg('OSMO-BTS-OCTPHY CONFIG:\n' + pprint.pformat(values))
        self.values = values
        with open(self.config_file, 'w') as f:
            r = template.render(OsmoBtsOctphy.CONF_BTS_OCTPHY, values)
            self.dbg(r)
            f.write(r)

    def conf_for_bsc(self):
        values = config.get_defaults('bsc_bts')
        config.overlay(values, config.get_defaults('osmo_bts_octphy'))
        config.overlay(values, self.conf)
        self.dbg(conf=values)
        return values

    def set_bsc(self, bsc):
        self.bsc = bsc

# vim: expandtab tabstop=4 shiftwidth=4