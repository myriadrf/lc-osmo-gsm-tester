# osmo_gsm_tester: manage resources
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
import copy
import atexit
import pprint

from . import log
from . import config
from . import util
from . import schema

from .util import is_dict, is_list

HASH_KEY = '_hash'
RESERVED_KEY = '_reserved_by'
USED_KEY = '_used'

RESERVED_RESOURCES_FILE = 'reserved_resources.state'

R_IP_ADDRESS = 'ip_address'
R_RUN_NODE = 'run_node'
R_BTS = 'bts'
R_ARFCN = 'arfcn'
R_MODEM = 'modem'
R_OSMOCON = 'osmocon_phone'
R_ENB = 'enb'

class ResourcesPool(log.Origin):
    _remember_to_free = None
    _registered_exit_handler = False

    def __init__(self):
        self.config_path = config.get_main_config_value(config.CFG_RESOURCES_CONF)
        self.state_dir = config.get_state_dir()
        super().__init__(log.C_CNF, conf=self.config_path, state=self.state_dir.path)
        self.read_conf()

    def read_conf(self):
        self.all_resources = Resources(config.read(self.config_path, schema.get_resources_schema()) or {})
        self.all_resources.set_hashes()

    def reserve(self, origin, want, modifiers):
        '''
        attempt to reserve the resources specified in the dict 'want' for
        'origin'. Obtain a lock on the resources lock dir, verify that all
        wanted resources are available, and if yes mark them as reserved.

        On success, return a reservation object which can be used to release
        the reservation. The reservation will be freed automatically on program
        exit, if not yet done manually.

        'origin' should be an Origin() instance.

        'want' is a dict matching RESOURCES_SCHEMA, used to specify what to
        reserve.

        'modifiers' is a dict matching RESOURCES_SCHEMA, it is overlaid on top
        of 'want'.

        If an entry has no attribute set, any of the resources may be
        reserved without further limitations.

        ResourcesPool may also be selected with narrowed down constraints.
        This would reserve one IP address, two modems, one BTS of type
        sysmo and one of type trx, plus 2 ARFCNs in the 1800 band:

         {
           'ip_address': [ {} ],
           'bts': [ { 'type': 'sysmo' }, { 'type': 'trx' } ],
           'arfcn': [ { 'band': 'GSM-1800' }, { 'band': 'GSM-1800' } ],
           'modem': [ {}, {} ],
         }
        '''
        schema.validate(want, schema.get_resources_schema())
        schema.validate(modifiers, schema.get_resources_schema())

        origin_id = origin.origin_id()

        with self.state_dir.lock(origin_id):
            rrfile_path = self.state_dir.mk_parentdir(RESERVED_RESOURCES_FILE)
            reserved = Resources(config.read(rrfile_path, if_missing_return={}))
            to_be_reserved = self.all_resources.without(reserved).find(origin, want)

            to_be_reserved.mark_reserved_by(origin_id)

            reserved.add(to_be_reserved)
            config.write(rrfile_path, reserved)

            self.remember_to_free(to_be_reserved)
            return ReservedResources(self, origin, to_be_reserved, modifiers)

    def free(self, origin, to_be_freed):
        log.ctx(origin)
        with self.state_dir.lock(origin.origin_id()):
            rrfile_path = self.state_dir.mk_parentdir(RESERVED_RESOURCES_FILE)
            reserved = Resources(config.read(rrfile_path, if_missing_return={}))
            reserved.drop(to_be_freed)
            config.write(rrfile_path, reserved)
            self.forget_freed(to_be_freed)

    def register_exit_handler(self):
        if self._registered_exit_handler:
            return
        atexit.register(self.clean_up_registered_resources)
        self._registered_exit_handler = True

    def unregister_exit_handler(self):
        if not self._registered_exit_handler:
            return
        atexit.unregister(self.clean_up_registered_resources)
        self._registered_exit_handler = False

    def clean_up_registered_resources(self):
        if not self._remember_to_free:
            return
        self.free(log.Origin('atexit.clean_up_registered_resources()'),
                  self._remember_to_free)

    def remember_to_free(self, to_be_reserved):
        self.register_exit_handler()
        if not self._remember_to_free:
            self._remember_to_free = Resources()
        self._remember_to_free.add(to_be_reserved)

    def forget_freed(self, freed):
        if freed is self._remember_to_free:
            self._remember_to_free.clear()
        else:
            self._remember_to_free.drop(freed)
        if not self._remember_to_free:
            self.unregister_exit_handler()

    def next_persistent_value(self, token, first_val, validate_func, inc_func, origin):
        origin_id = origin.origin_id()

        with self.state_dir.lock(origin_id):
            token_path = self.state_dir.child('last_used_%s.state' % token)
            log.ctx(token_path)
            last_value = first_val
            if os.path.exists(token_path):
                if not os.path.isfile(token_path):
                    raise RuntimeError('path should be a file but is not: %r' % token_path)
                with open(token_path, 'r') as f:
                    last_value = f.read().strip()
                validate_func(last_value)

            next_value = inc_func(last_value)
            with open(token_path, 'w') as f:
                f.write(next_value)
            return next_value

    def next_msisdn(self, origin):
        return self.next_persistent_value('msisdn', '1000', schema.msisdn, util.msisdn_inc, origin)

    def next_lac(self, origin):
        # LAC=0 has special meaning (MS detached), avoid it
        return self.next_persistent_value('lac', '1', schema.uint16, lambda x: str(((int(x)+1) % pow(2,16)) or 1), origin)

    def next_rac(self, origin):
        return self.next_persistent_value('rac', '1', schema.uint8, lambda x: str((int(x)+1) % pow(2,8) or 1), origin)

    def next_cellid(self, origin):
        return self.next_persistent_value('cellid', '1', schema.uint16, lambda x: str((int(x)+1) % pow(2,16)), origin)

    def next_bvci(self, origin):
        # BVCI=0 and =1 are reserved, avoid them.
        return self.next_persistent_value('bvci', '2', schema.uint16, lambda x: str(int(x)+1) if int(x) < pow(2,16) - 1 else '2', origin)

    def next_zmq_port_range(self, origin, num_ports):
        # Allocate continuous num_ports port between 2000 and 2200. returns base port.
        # Assumption: base port is always an odd number.
        num_ports = num_ports if num_ports % 2 == 0 else num_ports + 1
        base_port = self.next_persistent_value('bvci', '2000', schema.uint16,
                                               lambda x: str(int(x)+num_ports) if int(x) < 2200 - num_ports else '2000', origin)
        return int(base_port)


class NoResourceExn(log.Error):
    pass

class Resources(dict):

    def __init__(self, all_resources={}, do_copy=True):
        if do_copy:
            all_resources = copy.deepcopy(all_resources)
        self.update(all_resources)

    def drop(self, reserved, fail_if_not_found=True):
        # protect from modifying reserved because we're the same object
        if reserved is self:
            raise RuntimeError('Refusing to drop a list of resources from itself.'
                               ' This is probably a bug where a list of Resources()'
                               ' should have been copied but is passed as-is.'
                               ' use Resources.clear() instead.')

        for key, reserved_list in reserved.items():
            my_list = self.get(key) or []

            if my_list is reserved_list:
                self.pop(key)
                continue

            for reserved_item in reserved_list:
                found = False
                reserved_hash = reserved_item.get(HASH_KEY)
                if not reserved_hash:
                    raise RuntimeError('Resources.drop() only works with hashed items')

                for i in range(len(my_list)):
                    my_item = my_list[i]
                    my_hash = my_item.get(HASH_KEY)
                    if not my_hash:
                        raise RuntimeError('Resources.drop() only works with hashed items')
                    if my_hash == reserved_hash:
                        found = True
                        my_list.pop(i)
                        break

                if fail_if_not_found and not found:
                    raise RuntimeError('Asked to drop resource from a pool, but the'
                                       ' resource was not found: %s = %r' % (key, reserved_item))

            if not my_list:
                self.pop(key)
        return self

    def without(self, reserved):
        return Resources(self).drop(reserved)

    def find(self, for_origin, want, skip_if_marked=None, do_copy=True, raise_if_missing=True, log_label='Reserving'):
        '''
        Pass a dict of resource requirements, e.g.:
          want = {
            'bts': [ {'type': 'osmo-bts-sysmo',}, {} ],
            'modem': [ {}, {}, {} ]
          }
        This function tries to find a combination from the available resources that
        matches these requirements. The return value is a dict (wrapped in a Resources class)
        that contains the matching resources in the order of 'want' dict: in above
        example, the returned dict would have a 'bts' list with the first item being
        a sysmoBTS, the second item being any other available BTS.

        If skip_if_marked is passed, any resource that contains this key is skipped.
        E.g. if a BTS has the USED_KEY set like
          reserved_resources = { 'bts' : {..., '_used': True} }
        then this may be skipped by passing skip_if_marked='_used'
        (or rather skip_if_marked=USED_KEY).

        If do_copy is True, the returned dict is a deep copy and does not share
        lists with any other Resources dict.

        If raise_if_missing is False, this will return an empty item for any
        resource that had no match, instead of immediately raising an exception.

        This function expects input dictionaries whose contents have already
        been replicated based on its the 'times' attributes. See
        config.replicate_times() for more details.
        '''
        matches = {}
        for key, want_list in sorted(want.items()): # sorted for deterministic test results
            # here we have a resource of a given type, e.g. 'bts', with a list
            # containing as many BTSes as the caller wants to reserve/use. Each
            # list item contains specifics for the particular BTS.
            my_list = self.get(key, [])

            if log_label:
                for_origin.log(log_label, len(want_list), 'x', key, '(candidates: %d)'%len(my_list))

            # Try to avoid a less constrained item snatching away a resource
            # from a more detailed constrained requirement.

            # first record all matches, so that each requested item has a list
            # of all available resources that match it. Some resources may
            # appear for multiple requested items. Store matching indexes.
            all_matches = []
            for want_item in want_list:
                item_match_list = []
                for i in range(len(my_list)):
                    my_item = my_list[i]
                    if skip_if_marked and my_item.get(skip_if_marked):
                        continue
                    if item_matches(my_item, want_item):
                        item_match_list.append(i)
                if not item_match_list:
                    if raise_if_missing:
                        raise NoResourceExn('No matching resource available for %s = %r'
                                            % (key, want_item))
                    else:
                        # this one failed... see below
                        all_matches = []
                        break

                all_matches.append( item_match_list )

            if not all_matches:
                # ...this one failed. Makes no sense to solve resource
                # allocations, return an empty list for this key to mark
                # failure.
                matches[key] = []
                continue

            # figure out who gets what
            try:
                solution = solve(all_matches)
            except NotSolvable:
                # instead of a cryptic error message, raise an exception that
                # conveys meaning to the user.
                raise NoResourceExn('Could not resolve request to reserve resources: '
                                    '%d x %s with requirements: %r' % (len(want_list), key, want_list))
            picked = [ my_list[i] for i in solution if i is not None ]
            for_origin.dbg('Picked', config.tostr(picked))
            matches[key] = picked

        return Resources(matches, do_copy=do_copy)

    def set_hashes(self):
        for key, item_list in self.items():
            for item in item_list:
                item[HASH_KEY] = util.hash_obj(item, HASH_KEY, RESERVED_KEY, USED_KEY)

    def add(self, more):
        if more is self:
            raise RuntimeError('adding a list of resources to itself?')
        schema.add(self, copy.deepcopy(more))

    def mark_reserved_by(self, origin_id):
        for key, item_list in self.items():
            for item in item_list:
                item[RESERVED_KEY] = origin_id


class NotSolvable(Exception):
    pass

def solve(all_matches):
    '''
    all_matches shall be a list of index-lists.
    all_matches[i] is the list of indexes that item i can use.
    Return a solution so that each i gets a different index.
    solve([ [0, 1, 2],
            [0],
            [0, 2] ]) == [1, 0, 2]
    '''

    def all_differ(l):
        return len(set(l)) == len(l)

    def search_in_permutations(fixed=[]):
        idx = len(fixed)
        for i in range(len(all_matches[idx])):
            val = all_matches[idx][i]
            # don't add a val that's already in the list
            if val in fixed:
                continue
            l = list(fixed)
            l.append(val)
            if len(l) == len(all_matches):
                # found a solution
                return l
            # not at the end yet, add next digit
            r = search_in_permutations(l)
            if r:
                # nested search_in_permutations() call found a solution
                return r
        # this entire branch yielded no solution
        return None

    if not all_matches:
        raise RuntimeError('Cannot solve: no candidates')

    solution = search_in_permutations()
    if not solution:
        raise NotSolvable('The requested resource requirements are not solvable %r'
                          % all_matches)
    return solution


def contains_hash(list_of_dicts, a_hash):
    for d in list_of_dicts:
        if d.get(HASH_KEY) == a_hash:
            return True
    return False

def item_matches(item, wanted_item, ignore_keys=None):
    if is_dict(wanted_item):
        # match up two dicts
        if not isinstance(item, dict):
            return False
        for key, wanted_val in wanted_item.items():
            if ignore_keys and key in ignore_keys:
                continue
            if not item_matches(item.get(key), wanted_val, ignore_keys=ignore_keys):
                return False
        return True

    if is_list(wanted_item):
        if not is_list(item):
            return False
        # Validate that all elements in both lists are of the same type:
        t = util.list_validate_same_elem_type(wanted_item + item)
        if t is None:
            return True # both lists are empty, return
        # For lists of complex objects, we expect them to be sorted lists:
        if t in (dict, list, tuple):
            for i in range(max(len(wanted_item), len(item))):
                log.ctx(idx=i)
                subitem = item[i] if i < len(item) else util.empty_instance_type(t)
                wanted_subitem = wanted_item[i] if i < len(wanted_item) else util.empty_instance_type(t)
                if not item_matches(subitem, wanted_subitem, ignore_keys=ignore_keys):
                    return False
        else: # for lists of basic elements, we handle them as unsorted sets:
            for val in wanted_item:
                if val not in item:
                    return False
        return True

    return item == wanted_item


class ReservedResources(log.Origin):
    '''
    After all resources have been figured out, this is the API that a test case
    gets to interact with resources. From those resources that have been
    reserved for it, it can pick some to mark them as currently in use.
    Functions like nitb() provide a resource by automatically picking its
    dependencies from so far unused (but reserved) resource.
    '''

    def __init__(self, resources_pool, origin, reserved, modifiers):
        self.resources_pool = resources_pool
        self.origin = origin
        self.reserved_original = reserved
        self.reserved = copy.deepcopy(self.reserved_original)
        config.overlay(self.reserved, modifiers)

    def __repr__(self):
        return 'resources(%s)=%s' % (self.origin.name(), pprint.pformat(self.reserved))

    def get(self, kind, specifics=None):
        if specifics is None:
            specifics = {}
        self.dbg('requesting use of', kind, specifics=specifics)
        want = { kind: [specifics] }
        available_dict = self.reserved.find(self.origin, want, skip_if_marked=USED_KEY,
                                            do_copy=False, raise_if_missing=False,
                                            log_label='Using')
        available = available_dict.get(kind)
        self.dbg(available=len(available))
        if not available:
            # cook up a detailed error message for the current situation
            kind_reserved = self.reserved.get(kind, [])
            used_count = len([r for r in kind_reserved if USED_KEY in r])
            matching = self.reserved.find(self.origin, want, raise_if_missing=False, log_label=None).get(kind, [])
            if not matching:
                msg = 'none of the reserved resources matches requirements %r' % specifics
            elif not (used_count < len(kind_reserved)):
                msg = 'suite.conf reserved only %d x %r.' % (len(kind_reserved), kind)
            else:
                msg = ('No unused resource left that matches the requirements;'
                       ' Of reserved %d x %r, %d match the requirements, but all are already in use;'
                       ' Requirements: %r'
                       % (len(kind_reserved), kind, len(matching), specifics))
            raise NoResourceExn('When trying to use instance nr %d of %r: %s' % (used_count + 1, kind, msg))

        pick = available[0]
        self.dbg(using=pick)
        assert not pick.get(USED_KEY)
        pick[USED_KEY] = True
        return copy.deepcopy(pick)

    def put(self, item):
        if not item.get(USED_KEY):
            raise RuntimeError('Can only put() a resource that is used: %r' % item)
        hash_to_put = item.get(HASH_KEY)
        if not hash_to_put:
            raise RuntimeError('Can only put() a resource that has a hash marker: %r' % item)
        for key, item_list in self.reserved.items():
            my_list = self.get(key)
            for my_item in my_list:
                if hash_to_put == my_item.get(HASH_KEY):
                    my_item.pop(USED_KEY)

    def put_all(self):
        if not self.reserved:
            return
        for key, item_list in self.reserved.items():
            for item in item_list:
                item.pop(USED_KEY, None)

    def free(self):
        if self.reserved_original:
            self.resources_pool.free(self.origin, self.reserved_original)
        self.reserved_original = None

    def counts(self):
        counts = {}
        for key in self.reserved.keys():
            counts[key] = self.count(key)
        return counts

    def count(self, key):
        return len(self.reserved.get(key) or [])

# vim: expandtab tabstop=4 shiftwidth=4
