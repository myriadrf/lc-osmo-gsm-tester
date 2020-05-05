# osmo_gsm_tester: base classes to share code among eNodeB subclasses.
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

from abc import ABCMeta, abstractmethod
from ..core import log, config
from ..core import schema

def on_register_schemas():
    resource_schema = {
        'label': schema.STR,
        'type': schema.STR,
        'remote_user': schema.STR,
        'addr': schema.IPV4,
        'gtp_bind_addr': schema.IPV4,
        'id': schema.UINT,
        'num_prb': schema.UINT,
        'transmission_mode': schema.LTE_TRANSMISSION_MODE,
        'tx_gain': schema.UINT,
        'rx_gain': schema.UINT,
        'rf_dev_type': schema.STR,
        'rf_dev_args': schema.STR,
        'additional_args': schema.STR,
        'enable_measurements': schema.BOOL_STR,
        'a1_report_type': schema.STR,
        'a1_report_value': schema.INT,
        'a1_hysteresis': schema.INT,
        'a1_time_to_trigger': schema.INT,
        'a2_report_type': schema.STR,
        'a2_report_value': schema.INT,
        'a2_hysteresis': schema.INT,
        'a2_time_to_trigger': schema.INT,
        'a3_report_type': schema.STR,
        'a3_report_value': schema.INT,
        'a3_hysteresis': schema.INT,
        'a3_time_to_trigger': schema.INT,
        'num_cells': schema.UINT,
        'cell_list[].cell_id': schema.UINT,
        'cell_list[].pci': schema.UINT,
        'cell_list[].ncell_list[]': schema.UINT,
        'cell_list[].scell_list[]': schema.UINT,
        'cell_list[].dl_earfcn': schema.UINT,
        'cell_list[].dl_rfemu.type': schema.STR,
        'cell_list[].dl_rfemu.addr': schema.IPV4,
        'cell_list[].dl_rfemu.ports[]': schema.UINT,
        }
    schema.register_resource_schema('enb', resource_schema)

class eNodeB(log.Origin, metaclass=ABCMeta):

##############
# PROTECTED
##############
    def __init__(self, testenv, conf, name):
        super().__init__(log.C_RUN, '%s' % name)
        self._conf = conf
        self._addr = conf.get('addr', None)
        if self._addr is None:
            raise log.Error('addr not set')
        self._gtp_bind_addr = conf.get('gtp_bind_addr', None)
        if self._gtp_bind_addr is None:
            self._gtp_bind_addr = self._addr
        self.set_name('%s_%s' % (name, self._addr))
        self._txmode = 0
        self._id = None
        self._num_prb = 0
        self._num_cells = None
        self._epc = None

    def configure(self, config_specifics_li):
        values = dict(enb=config.get_defaults('enb'))
        for config_specifics in config_specifics_li:
            config.overlay(values, dict(enb=config.get_defaults(config_specifics)))
        config.overlay(values, dict(enb=self.testenv.suite().config().get('enb', {})))
        for config_specifics in config_specifics_li:
            config.overlay(values, dict(enb=self.testenv.suite().config().get(config_specifics, {})))
        config.overlay(values, dict(enb=self._conf))
        self._id = int(values['enb'].get('id', None))
        assert self._id is not None
        self._num_prb = int(values['enb'].get('num_prb', None))
        assert self._num_prb
        self._txmode = int(values['enb'].get('transmission_mode', None))
        assert self._txmode
        config.overlay(values, dict(enb={ 'num_ports': self.num_ports() }))
        assert self._epc is not None
        config.overlay(values, dict(enb={ 'mme_addr': self._epc.addr() }))
        config.overlay(values, dict(enb={ 'gtp_bind_addr': self._gtp_bind_addr }))
        self._num_cells = int(values['enb'].get('num_cells', None))
        assert self._num_cells

        # adjust cell_list to num_cells length:
        len_cell_list = len(values['enb']['cell_list'])
        if len_cell_list >= self._num_cells:
            values['enb']['cell_list'] = values['enb']['cell_list'][:self._num_cells]
        else:
            raise log.Error('enb.cell_list items (%d) < enb.num_cells (%d) attribute!' % (len_cell_list, self._num_cells))
        # adjust scell list (to only contain values available in cell_list):
        cell_id_list = [c['cell_id'] for c in values['enb']['cell_list']]
        for i in range(len(values['enb']['cell_list'])):
            scell_list_old = values['enb']['cell_list'][i]['scell_list']
            scell_list_new = []
            for scell_id in scell_list_old:
                if scell_id in cell_id_list:
                    scell_list_new.append(scell_id)
            values['enb']['cell_list'][i]['scell_list'] = scell_list_new

        return values

    def id(self):
        return self._id

    def num_ports(self):
        if self._txmode == 1:
            return 1
        return 2

########################
# PUBLIC - INTERNAL API
########################
    def cleanup(self):
        'Nothing to do by default. Subclass can override if required.'
        pass

    def num_prb(self):
        return self._num_prb

    #reference: srsLTE.git srslte_symbol_sz()
    def num_prb2symbol_sz(self, num_prb):
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

    def num_prb2base_srate(self, num_prb):
        return self.num_prb2symbol_sz(num_prb) * 15 * 1000

    def get_zmq_rf_dev_args(self):
        base_srate = self.num_prb2base_srate(self.num_prb())
        # Define all 8 possible RF ports (2x CA with 2x2 MIMO)
        rf_dev_args = 'fail_on_disconnect=true' \
                    + ',tx_port0=tcp://' + self.addr() + ':2000' \
                    + ',tx_port1=tcp://' + self.addr() + ':2002' \
                    + ',tx_port2=tcp://' + self.addr() + ':2004' \
                    + ',tx_port3=tcp://' + self.addr() + ':2006' \
                    + ',rx_port0=tcp://' + self.ue.addr() + ':2001' \
                    + ',rx_port1=tcp://' + self.ue.addr() + ':2003' \
                    + ',rx_port2=tcp://' + self.ue.addr() + ':2005' \
                    + ',rx_port3=tcp://' + self.ue.addr() + ':2007'

        if self._num_cells == 1:
            # Single carrier
            if self.num_ports() == 1:
                # SISO
                rf_dev_args += ',tx_freq0=2630e6,rx_freq0=2510e6'
            elif self.num_ports() == 2:
                # MIMO
                rf_dev_args += ',tx_freq0=2630e6,tx_freq1=2630e6,rx_freq0=2510e6,rx_freq1=2510e6'
        elif self._num_cells == 2:
            # 2x class
            if self.num_ports() == 1:
                # SISO
                rf_dev_args += ',tx_freq0=2630e6,tx_freq1=2650e6,rx_freq0=2510e6,rx_freq1=2530e6'
            elif self.num_ports() == 2:
                # MIMO
                rf_dev_args += ',tx_freq0=2630e6,tx_freq1=2630e6,tx_freq2=2650e6,tx_freq3=2650e6,rx_freq0=2510e6,rx_freq1=2510e6,rx_freq2=2530e6,rx_freq3=2530e6'

        rf_dev_args += ',id=enb,base_srate=' + str(base_srate)

        return rf_dev_args

    def get_instance_by_type(testenv, conf):
        """Allocate a ENB child class based on type. Opts are passed to the newly created object."""
        enb_type = conf.get('type')
        if enb_type is None:
            raise RuntimeError('ENB type is not defined!')

        if enb_type == 'amarisoftenb':
            from .enb_amarisoft import AmarisoftENB
            enb_class = AmarisoftENB
        elif enb_type == 'srsenb':
            from .enb_srs import srsENB
            enb_class = srsENB
        else:
            raise log.Error('ENB type not supported:', enb_type)
        return  enb_class(testenv, conf)

###################
# PUBLIC (test API included)
###################
    @abstractmethod
    def start(self, epc):
        'Starts ENB, it will connect to "epc"'
        pass

    @abstractmethod
    def ue_add(self, ue):
        pass

    @abstractmethod
    def running(self):
        pass

    @abstractmethod
    def ue_max_rate(self, downlink=True):
        pass

    @abstractmethod
    def get_rfemu(self, cell=0, dl=True):
        'Get rfemu.RFemulation subclass implementation object for given cell index and direction.'
        pass

    def addr(self):
        return self._addr

    def ue_max_rate(self, downlink=True):
        # The max rate for a single UE per PRB configuration in TM1
        max_phy_rate_tm1_dl = { 6 : 3.5e6,
                               15 : 11e6,
                               25 : 18e6,
                               50 : 36e6,
                               75 : 55e6,
                               100 : 75e6 }
        max_phy_rate_tm1_ul = { 6 : 0.9e6,
                               15 : 4.7e6,
                               25 : 10e6,
                               50 : 23e6,
                               75 : 34e6,
                               100 : 51e6 }
        if downlink:
            max_rate = max_phy_rate_tm1_dl[self.num_prb()]
        else:
            max_rate = max_phy_rate_tm1_ul[self.num_prb()]
        #TODO: calculate for non-standard prb numbers.
        if self._txmode > 2:
            max_rate *= 2
        # We use 3 control symbols for 6, 15 and 25 PRBs which results in lower max rate
        if self.num_prb() < 50:
          max_rate *= 0.9
        return max_rate

# vim: expandtab tabstop=4 shiftwidth=4