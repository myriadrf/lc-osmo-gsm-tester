# osmo_gsm_tester: specifics for running an osmo-hlr
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
import pprint
import sqlite3

from ..core import log, util, config, template, process
from . import pcap_recorder

class OsmoHlr(log.Origin):
    run_dir = None
    config_file = None
    process = None
    next_subscriber_id = 1

    def __init__(self, testenv, ip_address):
        super().__init__(log.C_RUN, 'osmo-hlr_%s' % ip_address.get('addr'))
        self.run_dir = None
        self.config_file = None
        self.process = None
        self.next_subscriber_id = 1
        self.testenv = testenv
        self.ip_address = ip_address

    def start(self):
        self.log('Starting osmo-hlr')
        self.run_dir = util.Dir(self.testenv.test().get_run_dir().new_dir(self.name()))
        self.configure()

        inst = util.Dir(os.path.abspath(self.testenv.suite().trial().get_inst('osmo-hlr')))

        binary = inst.child('bin', 'osmo-hlr')
        if not os.path.isfile(binary):
            raise log.Error('Binary missing:', binary)
        lib = inst.child('lib')
        if not os.path.isdir(lib):
            raise log.Error('No lib/ in', inst)

        # bootstrap an empty hlr.db
        self.db_file = self.run_dir.new_file('hlr.db')
        sql_input = inst.child('share/doc/osmo-hlr/sql/hlr.sql')
        if not os.path.isfile(sql_input):
            raise log.Error('hlr.sql missing:', sql_input)
        self.run_local('create_hlr_db', ('/bin/sh', '-c', 'sqlite3 %r < %r' % (self.db_file, sql_input)))

        pcap_recorder.PcapRecorder(self.testenv, self.run_dir.new_dir('pcap'), None,
                                   'host %s' % self.addr())

        env = { 'LD_LIBRARY_PATH': util.prepend_library_path(lib) }

        self.dbg(run_dir=self.run_dir, binary=binary, env=env)
        self.process = process.Process(self.name(), self.run_dir,
                                       (binary,
                                        '-c', os.path.abspath(self.config_file),
                                        '--database', self.db_file),
                                       env=env)
        self.testenv.remember_to_stop(self.process)
        self.process.launch()

    def configure(self):
        self.config_file = self.run_dir.new_file('osmo-hlr.cfg')
        self.dbg(config_file=self.config_file)

        values = dict(hlr=config.get_defaults('hlr'))
        config.overlay(values, self.testenv.suite().config())
        config.overlay(values, dict(hlr=dict(ip_address=self.ip_address)))

        self.dbg('HLR CONFIG:\n' + pprint.pformat(values))

        with open(self.config_file, 'w') as f:
            r = template.render('osmo-hlr.cfg', values)
            self.dbg(r)
            f.write(r)

    def addr(self):
        return self.ip_address.get('addr')

    def running(self):
        return not self.process.terminated()

    def run_local(self, name, popen_args):
        run_dir = self.run_dir.new_dir(name)
        proc = process.Process(name, run_dir, popen_args)
        proc.launch()
        proc.wait()
        if proc.result != 0:
            log.ctx(proc)
            raise log.Error('Exited in error')

    def subscriber_add(self, modem, msisdn=None, algo_str=None):
        if msisdn is None:
            msisdn = self.testenv.msisdn()
        modem.set_msisdn(msisdn)
        subscriber_id = self.next_subscriber_id
        self.next_subscriber_id += 1

        if algo_str is None:
            algo_str = modem.auth_algo() or util.OSMO_AUTH_ALGO_NONE

        if algo_str != util.OSMO_AUTH_ALGO_NONE and not modem.ki():
            raise log.Error("Auth algo %r selected but no KI specified" % algo_str)

        algo = util.osmo_auth_algo_by_name(algo_str)

        self.log('Add subscriber', msisdn=msisdn, imsi=modem.imsi(), subscriber_id=subscriber_id,
                 algo_str=algo_str, algo=algo)
        conn = sqlite3.connect(self.db_file)
        try:
            c = conn.cursor()
            c.execute('insert into subscriber (id, imsi, msisdn) values (?, ?, ?)',
                        (subscriber_id, modem.imsi(), modem.msisdn(),))
            c.execute('insert into auc_2g (subscriber_id, algo_id_2g, ki) values (?, ?, ?)',
                        (subscriber_id, algo, modem.ki(),))
            conn.commit()
        finally:
            conn.close()
        return subscriber_id

    def subscriber_delete(self, modem):
        self.log('Add subscriber', imsi=modem.imsi())
        conn = sqlite3.connect(self.db_file)
        try:
            c = conn.cursor()
            c.execute('select id from subscriber where imsi = ?', (modem.imsi(),))
            subscriber_id = c.fetchone()[0]
            c.execute('delete from subscriber where id = ?', (subscriber_id,))
            c.execute('delete from auc_2g where subscriber_id = ?', (subscriber_id,))
            conn.commit()
        finally:
            conn.close()

    def conf_for_client(self):
        return dict(hlr=dict(ip_address=self.ip_address))

# vim: expandtab tabstop=4 shiftwidth=4
