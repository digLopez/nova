# Copyright 2011 OpenStack Foundation
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

import datetime
import urlparse

from webob import exc

from nova.api.openstack import extensions
from nova.compute import api
from nova.compute import flavors
from nova import exception
from nova.openstack.common.gettextutils import _
from nova.openstack.common import timeutils

ALIAS = 'os-simple-tenant-usage'
authorize_show = extensions.extension_authorizer('compute',
                                                 'v3:' + ALIAS + ':show')
authorize_list = extensions.extension_authorizer('compute',
                                                 'v3:' + ALIAS + ':list')
VALID_DATETIME_FORMAT = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                         "%Y-%m-%d %H:%M:%S.%f"]


class SimpleTenantUsageController(object):
    def _hours_for(self, instance, period_start, period_stop):
        launched_at = instance['launched_at']
        terminated_at = instance['terminated_at']
        if terminated_at is not None:
            if not isinstance(terminated_at, datetime.datetime):
                terminated_at = timeutils.parse_strtime(terminated_at,
                                                        "%Y-%m-%d %H:%M:%S.%f")

        if launched_at is not None:
            if not isinstance(launched_at, datetime.datetime):
                launched_at = timeutils.parse_strtime(launched_at,
                                                      "%Y-%m-%d %H:%M:%S.%f")

        if terminated_at and terminated_at < period_start:
            return 0
        # nothing if it started after the usage report ended
        if launched_at and launched_at > period_stop:
            return 0
        if launched_at:
            # if instance launched after period_started, don't charge for first
            start = max(launched_at, period_start)
            if terminated_at:
                # if instance stopped before period_stop, don't charge after
                stop = min(period_stop, terminated_at)
            else:
                # instance is still running, so charge them up to current time
                stop = period_stop
            dt = stop - start
            seconds = (dt.days * 3600 * 24 + dt.seconds +
                       dt.microseconds / 100000.0)

            return seconds / 3600.0
        else:
            # instance hasn't launched, so no charge
            return 0

    def _get_flavor(self, context, compute_api, instance, flavors_cache):
        """Get flavor information from the instance's system_metadata,
        allowing a fallback to lookup by-id for deleted instances only.
        """
        try:
            return flavors.extract_flavor(instance)
        except KeyError:
            if not instance['deleted']:
                # Only support the fallback mechanism for deleted instances
                # that would have been skipped by migration #153
                raise

        flavor_type = instance['instance_type_id']
        if flavor_type in flavors_cache:
            return flavors_cache[flavor_type]

        try:
            it_ref = compute_api.get_instance_type(context, flavor_type)
            flavors_cache[flavor_type] = it_ref
        except exception.FlavorNotFound:
            # can't bill if there is no instance type
            it_ref = None

        return it_ref

    def _tenant_usages_for_period(self, context, period_start,
                                  period_stop, tenant_id=None, detailed=True):

        compute_api = api.API()
        instances = compute_api.get_active_by_window(context,
                                                     period_start,
                                                     period_stop,
                                                     tenant_id)
        rval = {}
        flavors = {}

        for instance in instances:
            info = {}
            info['hours'] = self._hours_for(instance,
                                            period_start,
                                            period_stop)
            flavor = self._get_flavor(context, compute_api, instance, flavors)
            if not flavor:
                continue

            info['instance_id'] = instance['uuid']
            info['name'] = instance['display_name']

            info['memory_mb'] = flavor['memory_mb']
            info['local_gb'] = flavor['root_gb'] + flavor['ephemeral_gb']
            info['vcpus'] = flavor['vcpus']

            info['tenant_id'] = instance['project_id']

            info['flavor'] = flavor['name']

            info['started_at'] = instance['launched_at']

            info['ended_at'] = instance['terminated_at']

            if info['ended_at']:
                info['state'] = 'terminated'
            else:
                info['state'] = instance['vm_state']

            now = timeutils.utcnow()

            if info['state'] == 'terminated':
                delta = info['ended_at'] - info['started_at']
            else:
                delta = now - info['started_at']

            info['uptime'] = delta.days * 24 * 3600 + delta.seconds

            if info['tenant_id'] not in rval:
                summary = {}
                summary['tenant_id'] = info['tenant_id']
                if detailed:
                    summary['server_usages'] = []
                summary['total_local_gb_usage'] = 0
                summary['total_vcpus_usage'] = 0
                summary['total_memory_mb_usage'] = 0
                summary['total_hours'] = 0
                summary['start'] = period_start
                summary['stop'] = period_stop
                rval[info['tenant_id']] = summary

            summary = rval[info['tenant_id']]
            summary['total_local_gb_usage'] += info['local_gb'] * info['hours']
            summary['total_vcpus_usage'] += info['vcpus'] * info['hours']
            summary['total_memory_mb_usage'] += (info['memory_mb'] *
                                                 info['hours'])

            summary['total_hours'] += info['hours']
            if detailed:
                summary['server_usages'].append(info)

        return rval.values()

    def _parse_datetime(self, dtstr):
        if not dtstr:
            return timeutils.utcnow()
        elif isinstance(dtstr, datetime.datetime):
            return dtstr
        for format in VALID_DATETIME_FORMAT:
            try:
                return timeutils.parse_strtime(dtstr, format)
            except ValueError:
                continue
        return None

    def _get_datetime_range(self, req):
        qs = req.environ.get('QUERY_STRING', '')
        env = urlparse.parse_qs(qs)
        # NOTE(lzyeval): env.get() always returns a list
        period_start = self._parse_datetime(env.get('start', [None])[0])
        if not period_start:
            msg = _("Start time is invalid format, valid "
                    "formats are %s") % VALID_DATETIME_FORMAT
            raise exc.HTTPBadRequest(explanation=msg)
        period_stop = self._parse_datetime(env.get('end', [None])[0])
        if not period_stop:
            msg = _("Stop time is invalid format, valid "
                    "formats are %s") % VALID_DATETIME_FORMAT
            raise exc.HTTPBadRequest(explanation=msg)
        if not period_start < period_stop:
            msg = _("Invalid start time. The start time cannot occur after "
                    "the end time.")
            raise exc.HTTPBadRequest(explanation=msg)

        detailed = env.get('detailed', ['0'])[0] == '1'
        return (period_start, period_stop, detailed)

    @extensions.expected_errors(400)
    def index(self, req):
        """Retrieve tenant_usage for all tenants."""
        context = req.environ['nova.context']

        authorize_list(context)

        (period_start, period_stop, detailed) = self._get_datetime_range(req)
        now = timeutils.utcnow()
        if period_stop > now:
            period_stop = now
        usages = self._tenant_usages_for_period(context,
                                                period_start,
                                                period_stop,
                                                detailed=detailed)
        return {'tenant_usages': usages}

    @extensions.expected_errors(400)
    def show(self, req, id):
        """Retrieve tenant_usage for a specified tenant."""
        tenant_id = id
        context = req.environ['nova.context']

        authorize_show(context, {'project_id': tenant_id})

        (period_start, period_stop, ignore) = self._get_datetime_range(req)
        now = timeutils.utcnow()
        if period_stop > now:
            period_stop = now
        usage = self._tenant_usages_for_period(context,
                                               period_start,
                                               period_stop,
                                               tenant_id=tenant_id,
                                               detailed=True)
        if len(usage):
            usage = usage[0]
        else:
            usage = {}
        return {'tenant_usage': usage}


class SimpleTenantUsage(extensions.V3APIExtensionBase):
    """Simple tenant usage extension."""

    name = "SimpleTenantUsage"
    alias = ALIAS
    version = 1

    def get_resources(self):
        res = [extensions.ResourceExtension('os-simple-tenant-usage',
                                           SimpleTenantUsageController())]
        return res

    def get_controller_extensions(self):
        """It's an abstract function V3APIExtensionBase and the extension
        will not be loaded without it.
        """
        return []
