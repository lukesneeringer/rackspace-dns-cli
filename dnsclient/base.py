# Copyright 2010 Jacob Kaplan-Moss
# Copyright 2011 OpenStack LLC.
# Copyright 2012 Kevin Minnick
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Base utilities to build API operation managers and objects on top of.
"""

import contextlib
import hashlib
import os
import time

from dnsclient import exceptions
from dnsclient import utils

def getid(obj):
    """
    Abstracts the common pattern of allowing both an object or an object's ID
    as a parameter when dealing with relationships.
    """
    try:
        return obj.id
    except AttributeError:
        return obj

class Manager(utils.HookableMixin):
    """
    Managers interact with a particular type of API (servers, flavors, images,
    etc.) and provide CRUD operations for them.
    """
    resource_class = None

    def __init__(self, api):
        self.api = api

    def _list(self, url, response_key, obj_class=None, body=None, offset=0):
        # if we were provided an offset, append it to the URL
        _url = url
        if offset:
            _url += '?offset=%d' % offset

        # retrieve the data from Rackspace
        if body:
            _resp, content = self.api.client.post(_url, body=body)
        else:
            _resp, content = self.api.client.get(_url)

        data = content[response_key]
        # NOTE(ja): keystone returns values as list as {'values': [ ... ]}
        #           unlike other services which just return the list...
        if isinstance(data, dict):
            try:
                data = data['values']
            except KeyError:
                pass

        # sanity check: are there multiple pages of data?
        # the Rackspace DNS API paginates, with a limit of 100 items per page,
        #   and sends down a link to request more
        # if we get such a link, we should continue to request more until
        #   there is no "next" link remaining, since the user asked to see
        #   the entire list, not just the first 100 entries
        if 'totalEntries' in content and content['totalEntries'] > 100 + offset:
            data += self._list(url, response_key, obj_class=obj_class, body=body, offset=offset + 100)

        # now, was this a recursive call to self._list?
        # if there were more than 100 items in the list, then this function was
        #   called recusrively, but we want a single list -- therefore, send back
        #   just the raw data, which will be appended to our caller's data list
        if offset:
            return data

        # okay, our list is complete, back to what we were doing
        if obj_class is None:
            obj_class = self.resource_class

        with self.completion_cache('human_id', obj_class, mode="w"):
            with self.completion_cache('uuid', obj_class, mode="w"):
                return [obj_class(self, res, loaded=True)
                        for res in data if res]

    @contextlib.contextmanager
    def completion_cache(self, cache_type, obj_class, mode):
        """
        The completion cache store items that can be used for bash
        autocompletion, like UUIDs or human-friendly IDs.

        A resource listing will clear and repopulate the cache.

        A resource create will append to the cache.

        Delete is not handled because listings are assumed to be performed
        often enough to keep the cache reasonably up-to-date.
        """
        base_dir = utils.env('NOVACLIENT_UUID_CACHE_DIR',
                             default="~/.novaclient")

        # NOTE(sirp): Keep separate UUID caches for each username + endpoint
        # pair
        username = utils.env('OS_USERNAME', 'NOVA_USERNAME')
        url = utils.env('OS_URL', 'NOVA_URL')
        uniqifier = hashlib.md5(username + url).hexdigest()

        cache_dir = os.path.expanduser(os.path.join(base_dir, uniqifier))

        try:
            os.makedirs(cache_dir, 0755)
        except OSError:
            # NOTE(kiall): This is typicaly either permission denied while
            #              attempting to create the directory, or the directory
            #              already exists. Either way, don't fail.
            pass

        resource = obj_class.__name__.lower()
        filename = "%s-%s-cache" % (resource, cache_type.replace('_', '-'))
        path = os.path.join(cache_dir, filename)

        cache_attr = "_%s_cache" % cache_type

        try:
            setattr(self, cache_attr, open(path, mode))
        except IOError:
            # NOTE(kiall): This is typicaly a permission denied while
            #              attempting to write the cache file.
            pass

        try:
            yield
        finally:
            cache = getattr(self, cache_attr, None)
            if cache:
                cache.close()
                delattr(self, cache_attr)

    def write_to_completion_cache(self, cache_type, val):
        cache = getattr(self, "_%s_cache" % cache_type, None)
        if cache:
            cache.write("%s\n" % val)

    def _get_async(self, url, response_key=None):
        async_resp = self._get(url, "")
        n = 0
        while async_resp.status == "RUNNING":
            time.sleep(1)
            async_resp = self._get("/status/%s" % async_resp.jobId, "")
            n = n+1
            if n > 10:
                break
 
        if async_resp.status == "ERROR":
            return self._get("/status/%s?showDetails=true" % async_resp.jobId, "error")
        else:
            return self._get("/status/%s?showDetails=true" % async_resp.jobId, "response")
        
    def _get(self, url, response_key=None):
        _resp, body = self.api.client.get(url)
        if response_key:
            return self.resource_class(self, body[response_key], loaded=True)
        else:
            return self.resource_class(self, body, loaded=True)

    def _create_async(self, url, body, response_key, return_raw=False, **kwargs):
        async_resp = self._create(url, body, response_key, return_raw, **kwargs)
        time.sleep(1)
        return self._get_async("/status/%s" % async_resp.jobId, "")

    def _create(self, url, body, response_key, return_raw=False, **kwargs):
        self.run_hooks('modify_body_for_create', body, **kwargs)
        _resp, body = self.api.client.post(url, body=body)
        if return_raw:
            return body[response_key]

        if response_key:
            with self.completion_cache('human_id', self.resource_class, mode="a"):
                with self.completion_cache('uuid', self.resource_class, mode="a"):
                    return self.resource_class(self, body[response_key])
        else:
            return self.resource_class(self, body, loaded=True)
        

    def _delete(self, url):
        _resp, _body = self.api.client.delete(url)

    def _update(self, url, body, **kwargs):
        self.run_hooks('modify_body_for_update', body, **kwargs)
        _resp, body = self.api.client.put(url, body=body)
        return body


class ManagerWithFind(Manager):
    """
    Like a `Manager`, but with additional `find()`/`findall()` methods.
    """
    def find(self, **kwargs):
        """
        Find a single item with attributes matching ``**kwargs``.

        This isn't very efficient: it loads the entire list then filters on
        the Python side.
        """
        matches = self.findall(**kwargs)
        num_matches = len(matches)
        if num_matches == 0:
            msg = "No %s matching %s." % (self.resource_class.__name__, kwargs)
            raise exceptions.NotFound(404, msg)
        elif num_matches > 1:
            raise exceptions.NoUniqueMatch
        else:
            return matches[0]

    def findall(self, **kwargs):
        """
        Find all items with attributes matching ``**kwargs``.

        This isn't very efficient: it loads the entire list then filters on
        the Python side.
        """
        found = []
        searches = kwargs.items()

        for obj in self.list():
            try:
                if all(getattr(obj, attr) == value
                                    for (attr, value) in searches):
                    found.append(obj)
            except AttributeError:
                continue

        return found

    def list(self):
        raise NotImplementedError

class Resource(object):
    """
    A resource represents a particular instance of an object (domain, record,
    etc). This is pretty much just a bag for attributes.

    :param manager: Manager object
    :param info: dictionary representing resource attributes
    :param loaded: prevent lazy-loading if set to True
    """
    HUMAN_ID = False
    NAME_ATTR = 'name'

    def __init__(self, manager, info, loaded=False):
        self.manager = manager
        self._info = info
        self._add_details(info)
        self._loaded = loaded

        # NOTE(sirp): ensure `id` is already present because if it isn't we'll
        # enter an infinite loop of __getattr__ -> get -> __init__ ->
        # __getattr__ -> ...
        if 'id' in self.__dict__ and len(str(self.id)) == 36:
            self.manager.write_to_completion_cache('uuid', self.id)

        human_id = self.human_id
        if human_id:
            self.manager.write_to_completion_cache('human_id', human_id)

    @property
    def human_id(self):
        """Subclasses may override this provide a pretty ID which can be used
        for bash completion.
        """
        if self.NAME_ATTR in self.__dict__ and self.HUMAN_ID:
            return utils.slugify(getattr(self, self.NAME_ATTR))
        return None

    def _add_details(self, info):
        for (k, v) in info.iteritems():
            try:
                setattr(self, k, v)
            except AttributeError:
                # In this case we already defined the attribute on the class
                pass

    def __getattr__(self, k):
        if k not in self.__dict__:
            #NOTE(bcwaldon): disallow lazy-loading if already loaded once
            if not self.is_loaded():
                self.get()
                return self.__getattr__(k)

            raise AttributeError(k)
        else:
            return self.__dict__[k]

    def __repr__(self):
        reprkeys = sorted(k for k in self.__dict__.keys() if k[0] != '_' and
                                                                k != 'manager')
        info = ", ".join("%s=%s" % (k, getattr(self, k)) for k in reprkeys)
        return "<%s %s>" % (self.__class__.__name__, info)

    def get(self):
        # set_loaded() first ... so if we have to bail, we know we tried.
        self.set_loaded(True)
        if not hasattr(self.manager, 'get'):
            return

        new = self.manager.get(self.id)
        if new:
            self._add_details(new._info)

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        if hasattr(self, 'id') and hasattr(other, 'id'):
            return self.id == other.id
        return self._info == other._info

    def is_loaded(self):
        return self._loaded

    def set_loaded(self, val):
        self._loaded = val