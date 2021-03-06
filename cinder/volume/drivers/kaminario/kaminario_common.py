# Copyright (c) 2016 by Kaminario Technologies, Ltd.
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
"""Volume driver for Kaminario K2 all-flash arrays."""

import math
import re
import threading

import eventlet
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import importutils
from oslo_utils import units
from oslo_utils import versionutils
import requests
import six

import cinder
from cinder import exception
from cinder.i18n import _, _LE, _LW, _LI
from cinder.objects import fields
from cinder import utils
from cinder.volume.drivers.san import san
from cinder.volume import utils as vol_utils

krest = importutils.try_import("krest")

K2_MIN_VERSION = '2.2.0'
K2_LOCK_PREFIX = 'Kaminario'
MAX_K2_RETRY = 5
LOG = logging.getLogger(__name__)

kaminario1_opts = [
    cfg.StrOpt('kaminario_nodedup_substring',
               default='K2-nodedup',
               help="If volume-type name contains this substring "
                    "nodedup volume will be created, otherwise "
                    "dedup volume wil be created.",
               deprecated_for_removal=True,
               deprecated_reason="This option is deprecated in favour of "
                                 "'kaminario:thin_prov_type' in extra-specs "
                                 "and will be removed in the next release.")]
kaminario2_opts = [
    cfg.BoolOpt('auto_calc_max_oversubscription_ratio',
                default=False,
                help="K2 driver will calculate max_oversubscription_ratio "
                     "on setting this option as True.")]

CONF = cfg.CONF
CONF.register_opts(kaminario1_opts)

K2HTTPError = requests.exceptions.HTTPError
K2_RETRY_ERRORS = ("MC_ERR_BUSY", "MC_ERR_BUSY_SPECIFIC",
                   "MC_ERR_INPROGRESS", "MC_ERR_START_TIMEOUT")

if krest:
    class KrestWrap(krest.EndPoint):
        def __init__(self, *args, **kwargs):
            self.krestlock = threading.Lock()
            super(KrestWrap, self).__init__(*args, **kwargs)

        def _should_retry(self, err_code, err_msg):
            if err_code == 400:
                for er in K2_RETRY_ERRORS:
                    if er in err_msg:
                        LOG.debug("Retry ERROR: %d with status %s",
                                  err_code, err_msg)
                        return True
            return False

        @utils.retry(exception.KaminarioRetryableException,
                     retries=MAX_K2_RETRY)
        def _request(self, method, *args, **kwargs):
            try:
                LOG.debug("running through the _request wrapper...")
                self.krestlock.acquire()
                return super(KrestWrap, self)._request(method,
                                                       *args, **kwargs)
            except K2HTTPError as err:
                err_code = err.response.status_code
                err_msg = err.response.text
                if self._should_retry(err_code, err_msg):
                    raise exception.KaminarioRetryableException(
                        reason=six.text_type(err_msg))
                raise
            finally:
                self.krestlock.release()


def kaminario_logger(func):
    """Return a function wrapper.

    The wrapper adds log for entry and exit to the function.
    """
    def func_wrapper(*args, **kwargs):
        LOG.debug('Entering %(function)s of %(class)s with arguments: '
                  ' %(args)s, %(kwargs)s',
                  {'class': args[0].__class__.__name__,
                   'function': func.__name__,
                   'args': args[1:],
                   'kwargs': kwargs})
        ret = func(*args, **kwargs)
        LOG.debug('Exiting %(function)s of %(class)s '
                  'having return value: %(ret)s',
                  {'class': args[0].__class__.__name__,
                   'function': func.__name__,
                   'ret': ret})
        return ret
    return func_wrapper


class Replication(object):
    def __init__(self, config, *args, **kwargs):
        self.backend_id = config.get('backend_id')
        self.login = config.get('login')
        self.password = config.get('password')
        self.rpo = config.get('rpo')


class KaminarioCinderDriver(cinder.volume.driver.ISCSIDriver):
    VENDOR = "Kaminario"
    stats = {}

    def __init__(self, *args, **kwargs):
        super(KaminarioCinderDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(san.san_opts)
        self.configuration.append_config_values(kaminario2_opts)
        self.replica = None
        self._protocol = None
        k2_lock_sfx = self.configuration.safe_get('volume_backend_name') or ''
        self.k2_lock_name = "%s-%s" % (K2_LOCK_PREFIX, k2_lock_sfx)

    def check_for_setup_error(self):
        if krest is None:
            msg = _("Unable to import 'krest' python module.")
            LOG.error(msg)
            raise exception.KaminarioCinderDriverException(reason=msg)
        else:
            conf = self.configuration
            self.client = KrestWrap(conf.san_ip,
                                    conf.san_login,
                                    conf.san_password,
                                    ssl_validate=False)
            if self.replica:
                self.target = KrestWrap(self.replica.backend_id,
                                        self.replica.login,
                                        self.replica.password,
                                        ssl_validate=False)
            v_rs = self.client.search("system/state")
            if hasattr(v_rs, 'hits') and v_rs.total != 0:
                ver = v_rs.hits[0].rest_api_version
                ver_exist = versionutils.convert_version_to_int(ver)
                ver_min = versionutils.convert_version_to_int(K2_MIN_VERSION)
                if ver_exist < ver_min:
                    msg = _("K2 rest api version should be "
                            ">= %s.") % K2_MIN_VERSION
                    LOG.error(msg)
                    raise exception.KaminarioCinderDriverException(reason=msg)

            else:
                msg = _("K2 rest api version search failed.")
                LOG.error(msg)
                raise exception.KaminarioCinderDriverException(reason=msg)

    @kaminario_logger
    def _check_ops(self):
        """Ensure that the options we care about are set."""
        required_ops = ['san_ip', 'san_login', 'san_password']
        for attr in required_ops:
            if not getattr(self.configuration, attr, None):
                raise exception.InvalidInput(reason=_('%s is not set.') % attr)

        replica = self.configuration.safe_get('replication_device')
        if replica and isinstance(replica, list):
            replica_ops = ['backend_id', 'login', 'password', 'rpo']
            for attr in replica_ops:
                if attr not in replica[0]:
                    msg = _('replication_device %s is not set.') % attr
                    raise exception.InvalidInput(reason=msg)
            self.replica = Replication(replica[0])

    @kaminario_logger
    def do_setup(self, context):
        super(KaminarioCinderDriver, self).do_setup(context)
        self._check_ops()

    @kaminario_logger
    def create_volume(self, volume):
        """Volume creation in K2 needs a volume group.

        - create a volume group
        - create a volume in the volume group
        """
        vg_name = self.get_volume_group_name(volume.id)
        vol_name = self.get_volume_name(volume.id)
        prov_type = self._get_is_dedup(volume.get('volume_type'))
        try:
            LOG.debug("Creating volume group with name: %(name)s, "
                      "quota: unlimited and dedup_support: %(dedup)s",
                      {'name': vg_name, 'dedup': prov_type})

            vg = self.client.new("volume_groups", name=vg_name, quota=0,
                                 is_dedup=prov_type).save()
            LOG.debug("Creating volume with name: %(name)s, size: %(size)s "
                      "GB, volume_group: %(vg)s",
                      {'name': vol_name, 'size': volume.size, 'vg': vg_name})
            vol = self.client.new("volumes", name=vol_name,
                                  size=volume.size * units.Mi,
                                  volume_group=vg).save()
        except Exception as ex:
            vg_rs = self.client.search("volume_groups", name=vg_name)
            if vg_rs.total != 0:
                LOG.debug("Deleting vg: %s for failed volume in K2.", vg_name)
                vg_rs.hits[0].delete()
            LOG.exception(_LE("Creation of volume %s failed."), vol_name)
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

        if self._get_is_replica(volume.volume_type) and self.replica:
            self._create_volume_replica(volume, vg, vol, self.replica.rpo)

    @kaminario_logger
    def _create_volume_replica(self, volume, vg, vol, rpo):
        """Volume replica creation in K2 needs session and remote volume.

        - create a session
        - create a volume in the volume group

        """
        session_name = self.get_session_name(volume.id)
        rsession_name = self.get_rep_name(session_name)

        rvg_name = self.get_rep_name(vg.name)
        rvol_name = self.get_rep_name(vol.name)

        k2peer_rs = self.client.search("replication/peer_k2arrays",
                                       mgmt_host=self.replica.backend_id)
        if hasattr(k2peer_rs, 'hits') and k2peer_rs.total != 0:
            k2peer = k2peer_rs.hits[0]
        else:
            msg = _("Unable to find K2peer in source K2:")
            LOG.error(msg)
            raise exception.KaminarioCinderDriverException(reason=msg)
        try:
            LOG.debug("Creating source session with name: %(sname)s and "
                      " target session name: %(tname)s",
                      {'sname': session_name, 'tname': rsession_name})
            src_ssn = self.client.new("replication/sessions")
            src_ssn.replication_peer_k2array = k2peer
            src_ssn.auto_configure_peer_volumes = "False"
            src_ssn.local_volume_group = vg
            src_ssn.replication_peer_volume_group_name = rvg_name
            src_ssn.remote_replication_session_name = rsession_name
            src_ssn.name = session_name
            src_ssn.rpo = rpo
            src_ssn.save()
            LOG.debug("Creating remote volume with name: %s",
                      rvol_name)
            self.client.new("replication/peer_volumes",
                            local_volume=vol,
                            name=rvol_name,
                            replication_session=src_ssn).save()
            src_ssn.state = "in_sync"
            src_ssn.save()
        except Exception as ex:
            LOG.exception(_LE("Replication for the volume %s has "
                              "failed."), vol.name)
            self._delete_by_ref(self.client, "replication/sessions",
                                session_name, 'session')
            self._delete_by_ref(self.target, "replication/sessions",
                                rsession_name, 'remote session')
            self._delete_by_ref(self.target, "volumes",
                                rvol_name, 'remote volume')
            self._delete_by_ref(self.client, "volumes", vol.name, "volume")
            self._delete_by_ref(self.target, "volume_groups",
                                rvg_name, "remote vg")
            self._delete_by_ref(self.client, "volume_groups", vg.name, "vg")
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    def _delete_by_ref(self, device, url, name, msg):
        rs = device.search(url, name=name)
        for result in rs.hits:
            result.delete()
            LOG.debug("Deleting %(msg)s: %(name)s", {'msg': msg, 'name': name})

    @kaminario_logger
    def _failover_volume(self, volume):
        """Promoting a secondary volume to primary volume."""
        session_name = self.get_session_name(volume.id)
        rsession_name = self.get_rep_name(session_name)
        tgt_ssn = self.target.search("replication/sessions",
                                     name=rsession_name).hits[0]
        if tgt_ssn.state == 'in_sync':
            tgt_ssn.state = 'failed_over'
            tgt_ssn.save()
            LOG.debug("The target session: %s state is "
                      "changed to failed_over ", rsession_name)

    @kaminario_logger
    def failover_host(self, context, volumes, secondary_id=None):
        """Failover to replication target."""
        volume_updates = []
        if secondary_id and secondary_id != self.replica.backend_id:
            LOG.error(_LE("Kaminario driver received failover_host "
                          "request, But backend is non replicated device"))
            raise exception.UnableToFailOver(reason=_("Failover requested "
                                                      "on non replicated "
                                                      "backend."))
        for v in volumes:
            vol_name = self.get_volume_name(v['id'])
            rv = self.get_rep_name(vol_name)
            if self.target.search("volumes", name=rv).total:
                self._failover_volume(v)
                volume_updates.append(
                    {'volume_id': v['id'],
                     'updates':
                     {'replication_status':
                      fields.ReplicationStatus.FAILED_OVER}})
            else:
                volume_updates.append({'volume_id': v['id'],
                                       'updates': {'status': 'error', }})

        return self.replica.backend_id, volume_updates

    @kaminario_logger
    def create_volume_from_snapshot(self, volume, snapshot):
        """Create volume from snapshot.

        - search for snapshot and retention_policy
        - create a view from snapshot and attach view
        - create a volume and attach volume
        - copy data from attached view to attached volume
        - detach volume and view and finally delete view
        """
        snap_name = self.get_snap_name(snapshot.id)
        view_name = self.get_view_name(volume.id)
        vol_name = self.get_volume_name(volume.id)
        cview = src_attach_info = dest_attach_info = None
        rpolicy = self.get_policy()
        properties = utils.brick_get_connector_properties()
        LOG.debug("Searching for snapshot: %s in K2.", snap_name)
        snap_rs = self.client.search("snapshots", short_name=snap_name)
        if hasattr(snap_rs, 'hits') and snap_rs.total != 0:
            snap = snap_rs.hits[0]
            LOG.debug("Creating a view: %(view)s from snapshot: %(snap)s",
                      {'view': view_name, 'snap': snap_name})
            try:
                cview = self.client.new("snapshots",
                                        short_name=view_name,
                                        source=snap, retention_policy=rpolicy,
                                        is_exposable=True).save()
            except Exception as ex:
                LOG.exception(_LE("Creating a view: %(view)s from snapshot: "
                                  "%(snap)s failed"), {"view": view_name,
                                                       "snap": snap_name})
                raise exception.KaminarioCinderDriverException(
                    reason=six.text_type(ex.message))

        else:
            msg = _("Snapshot: %s search failed in K2.") % snap_name
            LOG.error(msg)
            raise exception.KaminarioCinderDriverException(reason=msg)

        try:
            conn = self.initialize_connection(cview, properties)
            src_attach_info = self._connect_device(conn)
            self.create_volume(volume)
            conn = self.initialize_connection(volume, properties)
            dest_attach_info = self._connect_device(conn)
            vol_utils.copy_volume(src_attach_info['device']['path'],
                                  dest_attach_info['device']['path'],
                                  snapshot.volume.size * units.Ki,
                                  self.configuration.volume_dd_blocksize,
                                  sparse=True)
            self.terminate_connection(volume, properties)
            self.terminate_connection(cview, properties)
        except Exception as ex:
            self.terminate_connection(cview, properties)
            self.terminate_connection(volume, properties)
            cview.delete()
            self.delete_volume(volume)
            LOG.exception(_LE("Copy to volume: %(vol)s from view: %(view)s "
                              "failed"), {"vol": vol_name, "view": view_name})
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    @kaminario_logger
    def create_cloned_volume(self, volume, src_vref):
        """Create a clone from source volume.

        - attach source volume
        - create and attach new volume
        - copy data from attached source volume to attached new volume
        - detach both volumes
        """
        clone_name = self.get_volume_name(volume.id)
        src_name = self.get_volume_name(src_vref.id)
        src_vol = self.client.search("volumes", name=src_name)
        src_map = self.client.search("mappings", volume=src_vol)
        if src_map.total != 0:
            msg = _("K2 driver does not support clone of a attached volume. "
                    "To get this done, create a snapshot from the attached "
                    "volume and then create a volume from the snapshot.")
            LOG.error(msg)
            raise exception.KaminarioCinderDriverException(reason=msg)
        try:
            properties = utils.brick_get_connector_properties()
            conn = self.initialize_connection(src_vref, properties)
            src_attach_info = self._connect_device(conn)
            self.create_volume(volume)
            conn = self.initialize_connection(volume, properties)
            dest_attach_info = self._connect_device(conn)
            vol_utils.copy_volume(src_attach_info['device']['path'],
                                  dest_attach_info['device']['path'],
                                  src_vref.size * units.Ki,
                                  self.configuration.volume_dd_blocksize,
                                  sparse=True)

            self.terminate_connection(volume, properties)
            self.terminate_connection(src_vref, properties)
        except Exception as ex:
            self.terminate_connection(src_vref, properties)
            self.terminate_connection(volume, properties)
            self.delete_volume(volume)
            LOG.exception(_LE("Create a clone: %s failed."), clone_name)
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    @kaminario_logger
    def delete_volume(self, volume):
        """Volume in K2 exists in a volume group.

        - delete the volume
        - delete the corresponding volume group
        """
        vg_name = self.get_volume_group_name(volume.id)
        vol_name = self.get_volume_name(volume.id)
        try:
            if self._get_is_replica(volume.volume_type) and self.replica:
                self._delete_volume_replica(volume, vg_name, vol_name)

            LOG.debug("Searching and deleting volume: %s in K2.", vol_name)
            vol_rs = self.client.search("volumes", name=vol_name)
            if vol_rs.total != 0:
                vol_rs.hits[0].delete()
            LOG.debug("Searching and deleting vg: %s in K2.", vg_name)
            vg_rs = self.client.search("volume_groups", name=vg_name)
            if vg_rs.total != 0:
                vg_rs.hits[0].delete()
        except Exception as ex:
            LOG.exception(_LE("Deletion of volume %s failed."), vol_name)
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    @kaminario_logger
    def _delete_volume_replica(self, volume, vg_name, vol_name):
        rvg_name = self.get_rep_name(vg_name)
        rvol_name = self.get_rep_name(vol_name)
        session_name = self.get_session_name(volume.id)
        rsession_name = self.get_rep_name(session_name)
        src_ssn = self.client.search('replication/sessions',
                                     name=session_name).hits[0]
        tgt_ssn = self.target.search('replication/sessions',
                                     name=rsession_name).hits[0]
        src_ssn.state = 'suspended'
        src_ssn.save()
        self._check_for_status(tgt_ssn, 'suspended')
        src_ssn.state = 'idle'
        src_ssn.save()
        self._check_for_status(tgt_ssn, 'idle')
        tgt_ssn.delete()
        src_ssn.delete()

        LOG.debug("Searching and deleting snapshots for volume groups:"
                  "%(vg1)s, %(vg2)s in K2.", {'vg1': vg_name, 'vg2': rvg_name})
        vg = self.client.search('volume_groups', name=vg_name).hits
        rvg = self.target.search('volume_groups', name=rvg_name).hits
        snaps = self.client.search('snapshots', volume_group=vg).hits
        for s in snaps:
            s.delete()
        rsnaps = self.target.search('snapshots', volume_group=rvg).hits
        for s in rsnaps:
            s.delete()

        self._delete_by_ref(self.target, "volumes", rvol_name, 'remote volume')
        self._delete_by_ref(self.target, "volume_groups",
                            rvg_name, "remote vg")

    @kaminario_logger
    def _check_for_status(self, obj, status):
        while obj.state != status:
            obj.refresh()
            eventlet.sleep(1)

    @kaminario_logger
    def get_volume_stats(self, refresh=False):
        if refresh:
            self.update_volume_stats()
        stats = self.stats
        stats['storage_protocol'] = self._protocol
        stats['driver_version'] = self.VERSION
        stats['vendor_name'] = self.VENDOR
        backend_name = self.configuration.safe_get('volume_backend_name')
        stats['volume_backend_name'] = (backend_name or
                                        self.__class__.__name__)
        return stats

    def create_export(self, context, volume, connector):
        pass

    def ensure_export(self, context, volume):
        pass

    def remove_export(self, context, volume):
        pass

    @kaminario_logger
    def create_snapshot(self, snapshot):
        """Create a snapshot from a volume_group."""
        vg_name = self.get_volume_group_name(snapshot.volume_id)
        snap_name = self.get_snap_name(snapshot.id)
        rpolicy = self.get_policy()
        try:
            LOG.debug("Searching volume_group: %s in K2.", vg_name)
            vg = self.client.search("volume_groups", name=vg_name).hits[0]
            LOG.debug("Creating a snapshot: %(snap)s from vg: %(vg)s",
                      {'snap': snap_name, 'vg': vg_name})
            self.client.new("snapshots", short_name=snap_name,
                            source=vg, retention_policy=rpolicy,
                            is_auto_deleteable=False).save()
        except Exception as ex:
            LOG.exception(_LE("Creation of snapshot: %s failed."), snap_name)
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    @kaminario_logger
    def delete_snapshot(self, snapshot):
        """Delete a snapshot."""
        snap_name = self.get_snap_name(snapshot.id)
        try:
            LOG.debug("Searching and deleting snapshot: %s in K2.", snap_name)
            snap_rs = self.client.search("snapshots", short_name=snap_name)
            if snap_rs.total != 0:
                snap_rs.hits[0].delete()
        except Exception as ex:
            LOG.exception(_LE("Deletion of snapshot: %s failed."), snap_name)
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    @kaminario_logger
    def extend_volume(self, volume, new_size):
        """Extend volume."""
        vol_name = self.get_volume_name(volume.id)
        try:
            LOG.debug("Searching volume: %s in K2.", vol_name)
            vol = self.client.search("volumes", name=vol_name).hits[0]
            vol.size = new_size * units.Mi
            LOG.debug("Extending volume: %s in K2.", vol_name)
            vol.save()
        except Exception as ex:
            LOG.exception(_LE("Extending volume: %s failed."), vol_name)
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    @kaminario_logger
    def update_volume_stats(self):
        conf = self.configuration
        LOG.debug("Searching system capacity in K2.")
        cap = self.client.search("system/capacity").hits[0]
        LOG.debug("Searching total volumes in K2 for updating stats.")
        total_volumes = self.client.search("volumes").total - 1
        provisioned_vol = cap.provisioned_volumes
        if (conf.auto_calc_max_oversubscription_ratio and cap.provisioned
                and (cap.total - cap.free) != 0):
            ratio = provisioned_vol / float(cap.total - cap.free)
        else:
            ratio = conf.max_over_subscription_ratio
        self.stats = {'QoS_support': False,
                      'free_capacity_gb': cap.free / units.Mi,
                      'total_capacity_gb': cap.total / units.Mi,
                      'thin_provisioning_support': True,
                      'sparse_copy_volume': True,
                      'total_volumes': total_volumes,
                      'thick_provisioning_support': False,
                      'provisioned_capacity_gb': provisioned_vol / units.Mi,
                      'max_oversubscription_ratio': ratio,
                      'kaminario:thin_prov_type': 'dedup/nodedup',
                      'replication_enabled': True,
                      'kaminario:replication': True}

    @kaminario_logger
    def get_initiator_host_name(self, connector):
        """Return the initiator host name.

        Valid characters: 0-9, a-z, A-Z, '-', '_'
        All other characters are replaced with '_'.
        Total characters in initiator host name: 32
        """
        return re.sub('[^0-9a-zA-Z-_]', '_', connector.get('host', ''))[:32]

    @kaminario_logger
    def get_volume_group_name(self, vid):
        """Return the volume group name."""
        return "cvg-{0}".format(vid)

    @kaminario_logger
    def get_volume_name(self, vid):
        """Return the volume name."""
        return "cv-{0}".format(vid)

    @kaminario_logger
    def get_session_name(self, vid):
        """Return the volume name."""
        return "ssn-{0}".format(vid)

    @kaminario_logger
    def get_snap_name(self, sid):
        """Return the snapshot name."""
        return "cs-{0}".format(sid)

    @kaminario_logger
    def get_view_name(self, vid):
        """Return the view name."""
        return "cview-{0}".format(vid)

    @kaminario_logger
    def get_rep_name(self, name):
        """Return the corresponding replication names."""
        return "r{0}".format(name)

    @kaminario_logger
    def _delete_host_by_name(self, name):
        """Deleting host by name."""
        host_rs = self.client.search("hosts", name=name)
        if hasattr(host_rs, "hits") and host_rs.total != 0:
            host = host_rs.hits[0]
            host.delete()

    @kaminario_logger
    def get_policy(self):
        """Return the retention policy."""
        try:
            LOG.debug("Searching for retention_policy in K2.")
            return self.client.search("retention_policies",
                                      name="Best_Effort_Retention").hits[0]
        except Exception as ex:
            LOG.exception(_LE("Retention policy search failed in K2."))
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    @kaminario_logger
    def _get_volume_object(self, volume):
        vol_name = self.get_volume_name(volume.id)
        if volume.replication_status == 'failed-over':
            vol_name = self.get_rep_name(vol_name)
            self.client = self.target
        LOG.debug("Searching volume : %s in K2.", vol_name)
        vol_rs = self.client.search("volumes", name=vol_name)
        if not hasattr(vol_rs, 'hits') or vol_rs.total == 0:
            msg = _("Unable to find volume: %s from K2.") % vol_name
            LOG.error(msg)
            raise exception.KaminarioCinderDriverException(reason=msg)
        return vol_rs.hits[0]

    @kaminario_logger
    def _get_lun_number(self, vol, host):
        volsnap = None
        LOG.debug("Searching volsnaps in K2.")
        volsnap_rs = self.client.search("volsnaps", snapshot=vol)
        if hasattr(volsnap_rs, 'hits') and volsnap_rs.total != 0:
            volsnap = volsnap_rs.hits[0]

        LOG.debug("Searching mapping of volsnap in K2.")
        map_rs = self.client.search("mappings", volume=volsnap, host=host)
        return map_rs.hits[0].lun

    def initialize_connection(self, volume, connector):
        pass

    @kaminario_logger
    def terminate_connection(self, volume, connector):
        """Terminate connection of volume from host."""
        # Get volume object
        if type(volume).__name__ != 'RestObject':
            vol_name = self.get_volume_name(volume.id)
            if volume.replication_status == 'failed-over':
                vol_name = self.get_rep_name(vol_name)
                self.client = self.target
            LOG.debug("Searching volume: %s in K2.", vol_name)
            volume_rs = self.client.search("volumes", name=vol_name)
            if hasattr(volume_rs, "hits") and volume_rs.total != 0:
                volume = volume_rs.hits[0]
        else:
            vol_name = volume.name

        # Get host object.
        host_name = self.get_initiator_host_name(connector)
        host_rs = self.client.search("hosts", name=host_name)
        if hasattr(host_rs, "hits") and host_rs.total != 0 and volume:
            host = host_rs.hits[0]
            LOG.debug("Searching and deleting mapping of volume: %(name)s to "
                      "host: %(host)s", {'host': host_name, 'name': vol_name})
            map_rs = self.client.search("mappings", volume=volume, host=host)
            if hasattr(map_rs, "hits") and map_rs.total != 0:
                map_rs.hits[0].delete()
            if self.client.search("mappings", host=host).total == 0:
                LOG.debug("Deleting initiator hostname: %s in K2.", host_name)
                host.delete()
        else:
            LOG.warning(_LW("Host: %s not found on K2."), host_name)

    def k2_initialize_connection(self, volume, connector):
        # Get volume object.
        if type(volume).__name__ != 'RestObject':
            vol = self._get_volume_object(volume)
        else:
            vol = volume
        # Get host object.
        host, host_rs, host_name = self._get_host_object(connector)
        try:
            # Map volume object to host object.
            LOG.debug("Mapping volume: %(vol)s to host: %(host)s",
                      {'host': host_name, 'vol': vol.name})
            mapping = self.client.new("mappings", volume=vol, host=host).save()
        except Exception as ex:
            if host_rs.total == 0:
                self._delete_host_by_name(host_name)
            LOG.exception(_LE("Unable to map volume: %(vol)s to host: "
                              "%(host)s"), {'host': host_name,
                          'vol': vol.name})
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))
        # Get lun number.
        if type(volume).__name__ == 'RestObject':
            return self._get_lun_number(vol, host)
        else:
            return mapping.lun

    def _get_host_object(self, connector):
        pass

    def _get_is_dedup(self, vol_type):
        if vol_type:
            specs_val = vol_type.get('extra_specs', {}).get(
                'kaminario:thin_prov_type')
            if specs_val == 'nodedup':
                return False
            elif CONF.kaminario_nodedup_substring in vol_type.get('name'):
                LOG.info(_LI("'kaminario_nodedup_substring' option is "
                             "deprecated in favour of 'kaminario:thin_prov_"
                             "type' in extra-specs and will be removed in "
                             "the 10.0.0 release."))
                return False
            else:
                return True
        else:
            return True

    def _get_is_replica(self, vol_type):
        replica = False
        if vol_type and vol_type.get('extra_specs'):
            specs = vol_type.get('extra_specs')
            if (specs.get('kaminario:replication') == 'enabled' and
               self.replica):
                replica = True
        return replica

    def _get_replica_status(self, vg_name):
        vg = self.client.search("volume_groups", name=vg_name).hits[0]
        if self.client.search("replication/sessions",
                              local_volume_group=vg).total != 0:
            return True
        else:
            return False

    def manage_existing(self, volume, existing_ref):
        vol_name = existing_ref['source-name']
        new_name = self.get_volume_name(volume.id)
        vg_new_name = self.get_volume_group_name(volume.id)
        vg_name = None
        is_dedup = self._get_is_dedup(volume.get('volume_type'))
        try:
            LOG.debug("Searching volume: %s in K2.", vol_name)
            vol = self.client.search("volumes", name=vol_name).hits[0]
            vg = vol.volume_group
            vg_replica = self._get_replica_status(vg.name)
            vol_map = False
            if self.client.search("mappings", volume=vol).total != 0:
                vol_map = True
            if is_dedup != vg.is_dedup or vg_replica or vol_map:
                raise exception.ManageExistingInvalidReference(
                    existing_ref=existing_ref,
                    reason=_('Manage volume type invalid.'))
            vol.name = new_name
            vg_name = vg.name
            LOG.debug("Manage new volume name: %s", new_name)
            vg.name = vg_new_name
            LOG.debug("Manage volume group name: %s", vg_new_name)
            vg.save()
            LOG.debug("Manage volume: %s in K2.", vol_name)
            vol.save()
        except Exception as ex:
            vg_rs = self.client.search("volume_groups", name=vg_new_name)
            if hasattr(vg_rs, 'hits') and vg_rs.total != 0:
                vg = vg_rs.hits[0]
                if vg_name and vg.name == vg_new_name:
                    vg.name = vg_name
                    LOG.debug("Updating vg new name to old name: %s ", vg_name)
                    vg.save()
            LOG.exception(_LE("manage volume: %s failed."), vol_name)
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=six.text_type(ex.message))

    def manage_existing_get_size(self, volume, existing_ref):
        vol_name = existing_ref['source-name']
        v_rs = self.client.search("volumes", name=vol_name)
        if hasattr(v_rs, 'hits') and v_rs.total != 0:
            vol = v_rs.hits[0]
            size = vol.size / units.Mi
            return math.ceil(size)
        else:
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_('Unable to get size of manage volume.'))

    def after_volume_copy(self, ctxt, volume, new_volume, remote=None):
        self.delete_volume(volume)
        vg_name_old = self.get_volume_group_name(volume.id)
        vol_name_old = self.get_volume_name(volume.id)
        vg_name_new = self.get_volume_group_name(new_volume.id)
        vol_name_new = self.get_volume_name(new_volume.id)
        vg_new = self.client.search("volume_groups", name=vg_name_new).hits[0]
        vg_new.name = vg_name_old
        vg_new.save()
        vol_new = self.client.search("volumes", name=vol_name_new).hits[0]
        vol_new.name = vol_name_old
        vol_new.save()

    def retype(self, ctxt, volume, new_type, diff, host):
        old_type = volume.get('volume_type')
        vg_name = self.get_volume_group_name(volume.id)
        old_rep_type = self._get_replica_status(vg_name)
        new_rep_type = self._get_is_replica(new_type)
        new_prov_type = self._get_is_dedup(new_type)
        old_prov_type = self._get_is_dedup(old_type)
        # Change dedup<->nodedup with add/remove replication is complex in K2
        # since K2 does not have api to change dedup<->nodedup.
        if new_prov_type == old_prov_type:
            if not old_rep_type and new_rep_type:
                self._add_replication(volume)
                return True
            elif old_rep_type and not new_rep_type:
                self._delete_replication(volume)
                return True
        elif not new_rep_type and not old_rep_type:
            LOG.debug("Use '--migration-policy on-demand' to change 'dedup "
                      "without replication'<->'nodedup without replication'.")
            return False
        else:
            LOG.error(_LE('Change from type1: %(type1)s to type2: %(type2)s '
                          'is not supported directly in K2.'),
                      {'type1': old_type, 'type2': new_type})
            return False

    def _add_replication(self, volume):
        vg_name = self.get_volume_group_name(volume.id)
        vol_name = self.get_volume_name(volume.id)
        LOG.debug("Searching volume group with name: %(name)s",
                  {'name': vg_name})
        vg = self.client.search("volume_groups", name=vg_name).hits[0]
        LOG.debug("Searching volume with name: %(name)s",
                  {'name': vol_name})
        vol = self.client.search("volumes", name=vol_name).hits[0]
        self._create_volume_replica(volume, vg, vol, self.replica.rpo)

    def _delete_replication(self, volume):
        vg_name = self.get_volume_group_name(volume.id)
        vol_name = self.get_volume_name(volume.id)
        self._delete_volume_replica(volume, vg_name, vol_name)
