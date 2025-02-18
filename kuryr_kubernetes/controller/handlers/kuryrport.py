# Copyright 2020 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from openstack import exceptions as os_exc
from os_vif import objects
from oslo_config import cfg as oslo_cfg
from oslo_log import log as logging

from kuryr_kubernetes import clients
from kuryr_kubernetes import constants
from kuryr_kubernetes.controller.drivers import base as drivers
from kuryr_kubernetes.controller.drivers import utils as driver_utils
from kuryr_kubernetes import exceptions as k_exc
from kuryr_kubernetes.handlers import k8s_base
from kuryr_kubernetes import utils

LOG = logging.getLogger(__name__)
KURYRPORT_URI = constants.K8S_API_CRD_NAMESPACES + '/{ns}/kuryrports/{crd}'


class KuryrPortHandler(k8s_base.ResourceEventHandler):
    """Controller side of KuryrPort process for Kubernetes pods.

    `KuryrPortHandler` runs on the Kuryr-Kubernetes controller and is
    responsible for creating/removing the OpenStack resources associated to
    the newly created pods, namely ports and update the KuryrPort CRD data.
    """
    OBJECT_KIND = constants.K8S_OBJ_KURYRPORT
    OBJECT_WATCH_PATH = constants.K8S_API_CRD_KURYRPORTS

    def __init__(self):
        super(KuryrPortHandler, self).__init__()
        self._drv_project = drivers.PodProjectDriver.get_instance()
        self._drv_subnets = drivers.PodSubnetsDriver.get_instance()
        self._drv_sg = drivers.PodSecurityGroupsDriver.get_instance()
        # REVISIT(ltomasbo): The VIF Handler should not be aware of the pool
        # directly. Due to the lack of a mechanism to load and set the
        # VIFHandler driver, for now it is aware of the pool driver, but this
        # will be reverted as soon as a mechanism is in place.
        self._drv_vif_pool = drivers.VIFPoolDriver.get_instance(
            specific_driver='multi_pool')
        self._drv_vif_pool.set_vif_driver()
        self._drv_multi_vif = drivers.MultiVIFDriver.get_enabled_drivers()
        if self._is_network_policy_enabled():
            self._drv_lbaas = drivers.LBaaSDriver.get_instance()
            self._drv_svc_sg = (drivers.ServiceSecurityGroupsDriver
                                .get_instance())
        self.k8s = clients.get_kubernetes_client()

    def on_present(self, kuryrport_crd):
        if not kuryrport_crd['status']['vifs']:
            # Get vifs
            if not self.get_vifs(kuryrport_crd):
                # Ignore this event, according to one of the cases logged in
                # get_vifs method.
                return

        vifs = {ifname: {'default': data['default'],
                         'vif': objects.base.VersionedObject
                         .obj_from_primitive(data['vif'])}
                for ifname, data in kuryrport_crd['status']['vifs'].items()}

        if all([v['vif'].active for v in vifs.values()]):
            return

        changed = False

        try:
            for ifname, data in vifs.items():
                if (data['vif'].plugin == constants.KURYR_VIF_TYPE_SRIOV and
                        oslo_cfg.CONF.sriov.enable_node_annotations):
                    pod_node = kuryrport_crd['spec']['podNodeName']
                    # TODO(gryf): This probably will need adoption, so it will
                    # add information to CRD instead of the pod.
                    driver_utils.update_port_pci_info(pod_node, data['vif'])
                if not data['vif'].active:
                    try:
                        self._drv_vif_pool.activate_vif(data['vif'])
                        changed = True
                    except os_exc.ResourceNotFound:
                        LOG.debug("Port not found, possibly already deleted. "
                                  "No need to activate it")
        finally:
            if changed:
                try:
                    name = kuryrport_crd['metadata']['name']
                    namespace = kuryrport_crd['metadata']['namespace']
                    pod = self.k8s.get(f"{constants.K8S_API_NAMESPACES}"
                                       f"/{namespace}/pods/{name}")
                except k_exc.K8sResourceNotFound as ex:
                    LOG.exception("Failed to get pod: %s", ex)
                    raise

                project_id = self._drv_project.get_project(pod)

                try:
                    self._update_kuryrport_crd(kuryrport_crd, vifs)
                except k_exc.K8sResourceNotFound as ex:
                    LOG.exception("Failed to update KuryrPort CRD: %s", ex)
                    security_groups = self._drv_sg.get_security_groups(
                        pod, project_id)
                    for ifname, data in vifs.items():
                        self._drv_vif_pool.release_vif(pod, data['vif'],
                                                       project_id,
                                                       security_groups)
                except k_exc.K8sClientException:
                    raise k_exc.ResourceNotReady(pod['metadata']['name'])

                if self._is_network_policy_enabled():
                    crd_pod_selectors = self._drv_sg.create_sg_rules(pod)
                    if oslo_cfg.CONF.octavia_defaults.enforce_sg_rules:
                        services = driver_utils.get_services()
                        self._update_services(services, crd_pod_selectors,
                                              project_id)

    def on_finalize(self, kuryrport_crd):
        name = kuryrport_crd['metadata']['name']
        namespace = kuryrport_crd['metadata']['namespace']
        try:
            pod = self.k8s.get(f"{constants.K8S_API_NAMESPACES}"
                               f"/{namespace}/pods/{name}")
        except k_exc.K8sResourceNotFound:
            LOG.error("Pod %s/%s doesn't exists, deleting orphaned KuryrPort",
                      namespace, name)
            # TODO(gryf): Free resources
            try:
                self.k8s.remove_finalizer(kuryrport_crd,
                                          constants.KURYRPORT_FINALIZER)
            except k_exc.K8sClientException as ex:
                LOG.exception("Failed to remove finalizer from KuryrPort %s",
                              ex)
                raise
            return

        if ('deletionTimestamp' not in pod['metadata']):
            # NOTE(gryf): Ignore deleting KuryrPort, since most likely it was
            # removed manually, while we need vifs for corresponding pod
            # object which apperantely is still running.
            LOG.warning('Manually triggered KuryrPort %s removal. This '
                        'action should be avoided, since KuryrPort CRDs are '
                        'internal to Kuryr.', name)
            return

        project_id = self._drv_project.get_project(pod)
        try:
            crd_pod_selectors = self._drv_sg.delete_sg_rules(pod)
        except k_exc.ResourceNotReady:
            # NOTE(ltomasbo): If the pod is being deleted before
            # kuryr-controller annotated any information about the port
            # associated, there is no need for deleting sg rules associated to
            # it. So this exception could be safetly ignored for the current
            # sg drivers. Only the NP driver associates rules to the pods ips,
            # and that waits for annotations to start.
            #
            # NOTE(gryf): perhaps we don't need to handle this case, since
            # during CRD creation all the things, including security groups
            # rules would be created too.
            LOG.debug("Skipping SG rules deletion associated to the pod %s",
                      pod)
            crd_pod_selectors = []
        try:
            security_groups = self._drv_sg.get_security_groups(pod, project_id)
        except k_exc.ResourceNotReady:
            # NOTE(ltomasbo): If the namespace object gets deleted first the
            # namespace security group driver will raise a ResourceNotReady
            # exception as it cannot access anymore the kuryrnetwork CRD
            # annotated on the namespace object. In such case we set security
            # groups to empty list so that if pools are enabled they will be
            # properly released.
            security_groups = []

        for data in kuryrport_crd['status']['vifs'].values():
            vif = objects.base.VersionedObject.obj_from_primitive(data['vif'])
            self._drv_vif_pool.release_vif(pod, vif, project_id,
                                           security_groups)
        if (self._is_network_policy_enabled() and crd_pod_selectors and
                oslo_cfg.CONF.octavia_defaults.enforce_sg_rules):
            services = driver_utils.get_services()
            self._update_services(services, crd_pod_selectors, project_id)

        # Remove finalizer out of pod.
        self.k8s.remove_finalizer(pod, constants.POD_FINALIZER)

        # Finally, remove finalizer from KuryrPort CRD
        self.k8s.remove_finalizer(kuryrport_crd, constants.KURYRPORT_FINALIZER)

    def get_vifs(self, kuryrport_crd):
        try:
            pod = self.k8s.get(f"{constants.K8S_API_NAMESPACES}"
                               f"/{kuryrport_crd['metadata']['namespace']}"
                               f"/pods"
                               f"/{kuryrport_crd['metadata']['name']}")
        except k_exc.K8sResourceNotFound as ex:
            LOG.exception("Failed to get pod: %s", ex)
            # TODO(gryf): Release resources
            self.k8s.remove_finalizer(kuryrport_crd,
                                      constants.KURYRPORT_FINALIZER)
            raise

        project_id = self._drv_project.get_project(pod)
        security_groups = self._drv_sg.get_security_groups(pod, project_id)
        try:
            subnets = self._drv_subnets.get_subnets(pod, project_id)
        except (os_exc.ResourceNotFound, k_exc.K8sResourceNotFound):
            LOG.warning("Subnet does not exists. If namespace driver is "
                        "used, probably the namespace for the pod is "
                        "already deleted. So this pod does not need to "
                        "get a port as it will be deleted too. If the "
                        "default subnet driver is used, then you must "
                        "select an existing subnet to be used by Kuryr.")
            return False

        # Request the default interface of pod
        try:
            main_vif = self._drv_vif_pool.request_vif(pod, project_id,
                                                      subnets,
                                                      security_groups)
        except os_exc.ResourceNotFound:
            # NOTE(gryf): It might happen, that between getting security
            # groups above and requesting VIF, network policy is deleted,
            # hence we will get 404 from OpenStackSDK. Let's retry, to refresh
            # information regarding SG.
            LOG.warning("SG not found during VIF requesting. Retrying.")
            raise k_exc.ResourceNotReady(pod['metadata']['name'])

        if not main_vif:
            pod_name = pod['metadata']['name']
            LOG.warning("Ignoring event due to pod %s not being "
                        "scheduled yet.", pod_name)
            return False

        vifs = {constants.DEFAULT_IFNAME: {'default': True, 'vif': main_vif}}

        # Request the additional interfaces from multiple drivers
        index = 0
        for driver in self._drv_multi_vif:
            additional_vifs = driver.request_additional_vifs(pod, project_id,
                                                             security_groups)
            for index, vif in enumerate(additional_vifs, start=index+1):
                ifname = (oslo_cfg.CONF.kubernetes.additional_ifname_prefix +
                          str(index))
                vifs[ifname] = {'default': False, 'vif': vif}

        try:
            self._update_kuryrport_crd(kuryrport_crd, vifs)
        except k_exc.K8sClientException as ex:
            LOG.exception("Kubernetes Client Exception creating "
                          "KuryrPort CRD: %s", ex)
            for ifname, data in vifs.items():
                self._drv_vif_pool.release_vif(pod, data['vif'],
                                               project_id,
                                               security_groups)
        return True

    def _update_kuryrport_crd(self, kuryrport_crd, vifs):
        LOG.debug('Updatting CRD %s', kuryrport_crd["metadata"]["name"])
        vif_dict = {}
        for ifname, data in vifs.items():
            data['vif'].obj_reset_changes(recursive=True)
            vif_dict[ifname] = {'default': data['default'],
                                'vif': data['vif'].obj_to_primitive()}

        self.k8s.patch_crd('status', utils.get_res_link(kuryrport_crd),
                           {'vifs': vif_dict})

    def _is_network_policy_enabled(self):
        enabled_handlers = oslo_cfg.CONF.kubernetes.enabled_handlers
        svc_sg_driver = oslo_cfg.CONF.kubernetes.service_security_groups_driver
        return ('policy' in enabled_handlers and svc_sg_driver == 'policy')

    def _update_services(self, services, crd_pod_selectors, project_id):
        for service in services.get('items'):
            if not driver_utils.service_matches_affected_pods(
                    service, crd_pod_selectors):
                continue
            sgs = self._drv_svc_sg.get_security_groups(service,
                                                       project_id)
            self._drv_lbaas.update_lbaas_sg(service, sgs)
