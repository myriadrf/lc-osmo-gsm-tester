# osmo_gsm_tester: specifics for running an iperf3 client and server
#
# Copyright (C) 2018 by sysmocom - s.f.m.c. GmbH
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
import json

from ..core import log, util, config, process, remote
from ..core import schema
from . import pcap_recorder, run_node

def on_register_schemas():
    schema_types = {
        'iperf3_protocol': IPerf3Client.validate_protocol,
    }
    schema.register_schema_types(schema_types)
    config_schema = {
        'time': schema.DURATION,
        'protocol': 'iperf3_protocol',
        'packet_length' : schema.UINT,
        }
    schema.register_config_schema('iperf3cli', config_schema)

def iperf3_result_to_json(log_obj, data):
    try:
        # Drop non-interesting self-generated output before json:
        if not data.startswith('{\n'):
            data = "{\n" + data.split("\n{\n")[1]
        # Sometimes iperf3 provides 2 dictionaries, the 2nd one being an error about being interrupted (by us).
        # json parser doesn't support (raises exception) parsing several dictionaries at a time (not a valid json object).
        # We are only interested in the first dictionary, the regular results one:
        data = data.split("\n}")[0] + "\n}"
        j = json.loads(data)
        return j
    except Exception as e:
        log_obj.log('failed parsing iperf3 output: "%s"' % data)
        raise e

def print_result_node_udp(result, node_str):
    try:
        sum = result['end']['sum']
        print("Result %s:" % node_str)
        print("\tSUM: %d KB, %d kbps, %d seconds %d/%d lost" % (sum['bytes']/1000, sum['bits_per_second']/1000, sum['seconds'], sum['lost_packets'], sum['packets']))
    except Exception as e:
        print("Exception while using iperf3 %s results: %r" % (node_str, repr(result)))
        raise e

def print_result_node_tcp(result, node_str):
    try:
        sent = result['end']['sum_sent']
        recv = result['end']['sum_received']
        print("Result %s:" % node_str)
        print("\tSEND: %d KB, %d kbps, %d seconds (%s retrans)" % (sent['bytes']/1000, sent['bits_per_second']/1000, sent['seconds'], str(sent.get('retransmits', 'unknown'))))
        print("\tRECV: %d KB, %d kbps, %d seconds" % (recv['bytes']/1000, recv['bits_per_second']/1000, recv['seconds']))
    except Exception as e:
        print("Exception while using iperf3 %s results: %r" % (node_str, repr(result)))
        raise e

def get_received_mbps(result, isUdp=True):
    try:
        recv = result['end']['sum' if isUdp else 'sum_received']
        return recv['bits_per_second']/1e6
    except Exception as e:
        print("Exception while using iperf3 results: %r" % (repr(result)))
        raise e

class IPerf3Server(log.Origin):

    DEFAULT_SRV_PORT = 5003
    LOGFILE = 'iperf3_srv.json'
    REMOTE_DIR = '/tmp'

    def __init__(self, testenv, ip_address):
        super().__init__(log.C_RUN, 'iperf3-srv_%s' % ip_address.get('addr'))
        self.run_dir = None
        self.process = None
        self._run_node = None
        self.testenv = testenv
        self.ip_address = ip_address
        self._port = IPerf3Server.DEFAULT_SRV_PORT
        self.log_file = None
        self.rem_host = None
        self.remote_log_file = None
        self.log_copied = False
        self.logfile_supported = False # some older versions of iperf doesn't support --logfile arg

    def cleanup(self):
        if self.process is None:
            return
        if self.runs_locally() or not self.logfile_supported or self.log_copied:
            return
        # copy back files (may not exist, for instance if there was an early error of process):
        try:
            self.rem_host.scpfrom('scp-back-log', self.remote_log_file, self.log_file)
            self.log_copied = True
        except Exception as e:
            self.log(repr(e))

    def runs_locally(self):
        locally = not self._run_node or self._run_node.is_local()
        return locally

    def start(self):
        self.log('Starting iperf3-srv')
        self.log_copied = False
        self.run_dir = util.Dir(self.testenv.test().get_run_dir().new_dir(self.name()))
        self.log_file = self.run_dir.new_child(IPerf3Server.LOGFILE)
        if self.runs_locally():
            self.start_locally()
        else:
            self.start_remotely()

    def start_remotely(self):
        self.rem_host = remote.RemoteHost(self.run_dir, self._run_node.ssh_user(), self._run_node.ssh_addr())
        remote_prefix_dir = util.Dir(IPerf3Server.REMOTE_DIR)
        remote_run_dir = util.Dir(remote_prefix_dir.child('srv-' + str(self)))
        self.remote_log_file = remote_run_dir.child(IPerf3Server.LOGFILE)

        self.rem_host.recreate_remote_dir(remote_run_dir)

        args = ('iperf3', '-s', '-B', self.addr(),
                '-p', str(self._port), '-J')
        if self.logfile_supported:
            args += ('--logfile', self.remote_log_file,)

        self.process = self.rem_host.RemoteProcess(self.name(), args)
        self.testenv.remember_to_stop(self.process)
        self.process.launch()

    def start_locally(self):
        pcap_recorder.PcapRecorder(self.testenv, self.run_dir.new_dir('pcap'), None,
                                   'host %s and port not 22' % self.addr())

        args = ('iperf3', '-s', '-B', self.addr(),
                '-p', str(self._port), '-J')
        if self.logfile_supported:
            args += ('--logfile', os.path.abspath(self.log_file),)

        self.process = process.Process(self.name(), self.run_dir, args, env={})
        self.testenv.remember_to_stop(self.process)
        self.process.launch()

    def set_run_node(self, run_node):
        self._run_node = run_node

    def set_port(self, port):
        self._port = port

    def stop(self):
        self.testenv.stop_process(self.process)

    def get_results(self):
        if self.logfile_supported:
            if not self.runs_locally() and not self.log_copied:
                self.rem_host.scpfrom('scp-back-log', self.remote_log_file, self.log_file)
                self.log_copied = True
            with open(self.log_file) as f:
                return iperf3_result_to_json(self, f.read())
        else:
            return iperf3_result_to_json(self, self.process.get_stdout())

    def print_results(self, client_was_udp):
        if client_was_udp:
            print_result_node_udp(self.get_results(), 'server')
        else:
            print_result_node_tcp(self.get_results(), 'server')

    def get_received_mbps(self, client_was_udp):
        return get_received_mbps(self.get_results(), client_was_udp)

    def addr(self):
        return self.ip_address.get('addr')

    def port(self):
        return self._port

    def __str__(self):
        return "%s:%u" %(self.addr(), self.port())

    def running(self):
        return not self.process.terminated()

    def create_client(self):
        return IPerf3Client(self.testenv, self)

class IPerf3Client(log.Origin):

    REMOTE_DIR = '/tmp'
    LOGFILE = 'iperf3_cli.json'

    PROTO_TCP = "tcp"
    PROTO_UDP = "udp"

    DIR_UL = "ul"
    DIR_DL = "dl"
    DIR_BI = "bi"

    @classmethod
    def validate_protocol(cls, val):
        return val in (cls.PROTO_TCP, cls.PROTO_UDP)

    def __init__(self, testenv, iperf3srv):
        super().__init__(log.C_RUN, 'iperf3-cli_%s' % iperf3srv.addr())
        self.run_dir = None
        self.process = None
        self._run_node = None
        self.server = iperf3srv
        self.testenv = testenv
        self._proto = None
        self._time_sec = None
        self.log_file = None
        self.rem_host = None
        self.remote_log_file = None
        self.log_copied = False
        self.logfile_supported = False # some older versions of iperf doesn't support --logfile arg
        self.is_android_ue = False

    def runs_locally(self):
        locally = not self._run_node or self._run_node.is_local()
        return locally

    def prepare_test_proc(self, dir=None, netns=None, time_sec=None, proto=None, bitrate=0, tos=None):
        values = config.get_defaults('iperf3cli')
        config.overlay(values, self.testenv.suite().config().get('iperf3cli', {}))

        if dir is None:
            dir = self.DIR_UL

        if time_sec is None:
            time_sec_str = values.get('time', time_sec)

            # Convert duration to seconds
            if isinstance(time_sec_str, str) and time_sec_str.endswith('h'):
                time_sec = int(time_sec_str[:-1]) * 3600
            elif isinstance(time_sec_str, str) and time_sec_str.endswith('m'):
                time_sec = int(time_sec_str[:-1]) * 60
            else:
                time_sec = int(time_sec_str)
        assert(time_sec)
        self._time_sec = time_sec

        if proto is None:
            proto = values.get('protocol', IPerf3Client.PROTO_TCP)
        self._proto = proto

        self.log('Preparing iperf3-client connecting to %s:%d (proto=%s,time=%ds)' % (self.server.addr(), self.server.port(), self._proto, time_sec))
        self.log_copied = False
        self.run_dir = util.Dir(self.testenv.test().get_run_dir().new_dir(self.name()))
        self.log_file = self.run_dir.new_child(IPerf3Client.LOGFILE)

        popen_args = ('iperf3', '-c',  self.server.addr(),
                      '-p', str(self.server.port()), '-J',
                      '-t', str(time_sec))
        if dir == IPerf3Client.DIR_DL:
            popen_args += ('-R',)
        elif dir == IPerf3Client.DIR_BI:
            popen_args += ('--bidir',)
        if proto == IPerf3Client.PROTO_UDP:
            popen_args += ('-u', '-b', str(bitrate))
            # Add the buffer length.
            if values.get('packet_length'):
                packet_length = str(values.get('packet_length'))
                popen_args += ('-l', packet_length)
        if tos is not None:
            popen_args += ('-S', str(tos))

        if self.runs_locally():
            proc = self.prepare_test_proc_locally(netns, popen_args)
        else:
            proc = self.prepare_test_proc_remotely(netns, popen_args)
        proc.set_default_wait_timeout(time_sec + 120) # leave extra time for remote run, ctrl conn establishment, buffer draining, etc.
        return proc

    def prepare_test_proc_remotely(self, netns, popen_args):
        self.rem_host = remote.RemoteHost(self.run_dir, self._run_node.ssh_user(), self._run_node.ssh_addr(), None,
                                          self._run_node.ssh_port())

        remote_prefix_dir = util.Dir(IPerf3Client.REMOTE_DIR)
        remote_run_dir = util.Dir(remote_prefix_dir.child('cli-' + str(self)))
        self.remote_log_file = remote_run_dir.child(IPerf3Client.LOGFILE)

        self.rem_host.recreate_remote_dir(remote_run_dir)

        if self.logfile_supported:
            popen_args += ('--logfile', self.remote_log_file,)

        if netns:
            self.process = self.rem_host.RemoteNetNSProcess(self.name(), netns, popen_args, env={})
        else:
            self.process = self.rem_host.RemoteProcess(self.name(), popen_args, env={})
        return self.process

    def prepare_test_proc_locally(self, netns, popen_args):
        pcap_recorder.PcapRecorder(self.testenv, self.run_dir.new_dir('pcap'), None,
                                   'host %s and port not 22' % self.server.addr(), netns)

        if self.logfile_supported:
            popen_args += ('--logfile', os.path.abspath(self.log_file),)

        if netns:
            self.process = process.NetNSProcess(self.name(), self.run_dir, netns, popen_args, env={})
        elif self._run_node.adb_serial_id():
            self.process = process.AdbProcess(self.name(), self.run_dir, self._run_node.adb_serial_id(), popen_args, env={})
        else:
            self.process = process.Process(self.name(), self.run_dir, popen_args, env={})
        return self.process

    def run_test_sync(self, netns=None):
        self.prepare_test_proc(netns)
        self.process.launch_sync()
        return self.get_results()

    def get_results(self):
        if self.logfile_supported:
            if not self.runs_locally() and not self.log_copied:
                self.rem_host.scpfrom('scp-back-log', self.remote_log_file, self.log_file)
                self.log_copied = True
            with open(self.log_file) as f:
                return iperf3_result_to_json(self, f.read())
        else:
            return iperf3_result_to_json(self, self.process.get_stdout())

    def print_results(self):
        if self.proto() == self.PROTO_UDP:
            print_result_node_udp(self.get_results(), 'client')
        else:
            print_result_node_tcp(self.get_results(), 'client')

    def get_received_mbps(self):
        if self.proto() == self.PROTO_UDP:
            return get_received_mbps(self.get_results(), isUdp=True)
        else:
            return get_received_mbps(self.get_results(), isUdp=False)

    def set_run_node(self, run_node):
        self._run_node = run_node

    def proto(self):
        return self._proto

    def time_sec(self):
        return self._time_sec

    def __str__(self):
        # FIXME: somehow differentiate between several clients connected to same server?
        return "%s:%u" %(self.server.addr(), self.server.port())

# vim: expandtab tabstop=4 shiftwidth=4
