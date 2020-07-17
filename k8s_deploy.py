"""
Install the following packages on your CB:
  pip install kubernetes
  pip install azure-cli

Install kubectl:
  az aks install-cli

Configure Azure login creds:
  az login        # this is a one time process

Confirm cluster access:
  az aks list  - # lists all clusters

"""

from __future__ import unicode_literals
import hashlib
import time

from django.urls import reverse
from kubernetes.client import Configuration, ApiClient
from kubernetes import config, client
from resourcehandlers.azure_arm.models import AzureARMHandler
from azure.mgmt.resource import ResourceManagementClient
from azure.common.credentials import ServicePrincipalCredentials
from azure.mgmt.resource.resources.models import ResourceGroup
from azure.mgmt.containerservice.models import ManagedClusterAgentPoolProfile
from azure.mgmt.containerservice.models import ManagedCluster
from containerorchestrators.models import ContainerOrchestratorTechnology
from containerorchestrators.kuberneteshandler.models import Kubernetes
from azure.mgmt.containerservice import ContainerServiceClient
from infrastructure.models import CustomField, Environment, Server
from portals.models import PortalConfig
from common.methods import set_progress
from resources.models import ResourceType, Resource
from azure.mgmt.containerservice.models import ManagedClusterServicePrincipalProfile
import subprocess
import yaml
import json
from threading import Timer
from msrest.polling import *

ENV_ID = '{{ cb_environment }}'
resource_group = '{{ resource_groups }}'
dns_prefix = '{{ cluster_dns_prefix }}'
agent_pool_name = '{{ cluster_pool_name}}'
CLUSTER_NAME = '{{ cluster_name }}'

try:
    environment = Environment.objects.get(id=ENV_ID)
    handler = environment.resource_handler.cast()
    client_id = handler.client_id
    secret = handler.secret
    location = handler.get_env_location(environment)
except:
    set_progress("Couldn't get environment.")

service = "lb-front"
image = 'nginx:1.15'

try:
    NODE_COUNT = int('{{ node_count }}')
except ValueError:
    NODE_COUNT = 1
TIMEOUT = 600  # 10 minutes


def get_credentials():
    creds = ServicePrincipalCredentials(
        client_id=handler.client_id,
        secret=handler.secret,
        tenant=handler.tenant_id,
    )
    return creds


def get_service_profile():
    profile = ManagedClusterServicePrincipalProfile(
        client_id=handler.client_id,
        secret=handler.secret,
    )
    return profile


def get_resource_client():
    credentials = get_credentials()
    subscription_id = handler.serviceaccount
    resource_client = ResourceManagementClient(credentials, subscription_id)
    return resource_client


def get_container_client():
    subscription_id = handler.serviceaccount
    credentials = get_credentials()
    container_client = ContainerServiceClient(credentials, subscription_id)
    return container_client


def create_resource_group():
    resource_client = get_resource_client()
    result = resource_client.resource_groups.create_or_update(
        resource_group,
        parameters=ResourceGroup(
            location=location,
            tags={},
        )
    )
    return result


def create_cluster(node_count):
    """
    Azure requires node pool name to be 9 alphanumeric characters, and lowercase.
    """
    profile = get_service_profile()
    container_client = get_container_client()
    cluster_resource = container_client.managed_clusters.create_or_update(
        resource_group,
        CLUSTER_NAME,
        parameters=ManagedCluster(
            location=location,
            tags=None,
            dns_prefix=dns_prefix.lower(),
            service_principal_profile=profile,
            agent_pool_profiles=[{
                'name': agent_pool_name.lower()[:9],
                'vm_size': 'Standard_DS2_v2',
                'count': node_count,
            }],
        ),
    )
    return cluster_resource


def create_deployment_object():
    """
    Creates the LB service and exposes the external IP
    """
    container = client.V1Container(
        name="my-nginx",
        image="nginx:1.15",
        ports=[client.V1ContainerPort(container_port=80)])
    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": "nginx"}),
        spec=client.V1PodSpec(containers=[container]))
    spec = client.ExtensionsV1beta1DeploymentSpec(
        replicas=1,
        template=template)
    set_progress("Instantiate the deployment object")
    deployment = client.ExtensionsV1beta1Deployment(
        api_version="extensions/v1beta1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name="nginx-deployment"),
        spec=spec)
    time.sleep(60)
    return deployment


def create_deployment(api_instance, deployment):
    api_response = api_instance.create_namespaced_deployment(body=deployment, namespace="default")
    time.sleep(60)
    return ("SUCCESS", "Deployment created. Status={}".format(api_response.status), "")


def create_service():
    api = client.CoreV1Api()
    body = {
        'apiVersion': 'v1',
        'kind': 'Service',
        'metadata': {
            'name': service,
        },
        'spec': {
            'type': 'LoadBalancer',
            'selector': {
                'app': service,
            },
            'ports': [
                client.V1ServicePort(port=80, target_port=80),
            ],
            'restartPolicy': 'Never',
            'serviceAccountName': "default",
        }
    }
    result = api.create_namespaced_service(namespace='default', body=body)
    return result


def get_cluster():
    result = subprocess.check_output(['az', 'aks', 'show', '-g', resource_group, '-n', CLUSTER_NAME, '-o', 'json'])
    res = json.loads(result)
    cluster = res['name']
    return cluster


def get_cluster_endpoint():
    '''
    Get kubernetes services and Load balancer endpoint from shell
    '''
    try:
        cluster = subprocess.check_output("kubectl get service -o json".split())
        res = json.loads(cluster)
        endpoint = res['items'][1]['status']['loadBalancer']['ingress'][0]['ip']
    except (subprocess.CalledProcessError, json.decoder.JSONDecodeError, KeyError):
        endpoint = None
    return endpoint


def wait_for_endpoint(timeout=None):
    endpoint = None
    start = time.time()
    while not endpoint:
        if timeout is not None and (time.time() - start > timeout):
            break
        endpoint = get_cluster_endpoint()
        time.sleep(5)
    return endpoint


def get_nodes():
    node = subprocess.check_output("kubectl get nodes -o json".split())
    res = json.loads(node)
    result = res['items']
    nodes = []
    for item in result:
        response = item["status"]["addresses"][1]["address"]
        nodes.append(response)
    return nodes


def wait_for_nodes(node_count, timeout=None):
    result = get_nodes()
    nodes = []
    start = time.time()
    while len(nodes) < node_count:
        if timeout is not None and (time.time() - start > timeout):
            break
        nodes = result or []
        time.sleep(5)
    return nodes


def wait_for_running_status(timeout=None):
    status = ''
    start = time.time()
    while status != "Succeeded":
        if status == 'Failed':
            raise CBException("Deployment failed")
        if timeout is not None and (time.time() - start > timeout):
            break
        cluster = subprocess.check_output(['az', 'aks', 'show', '-g', resource_group, '-n', CLUSTER_NAME, '-o', 'json'])
        res = json.loads(cluster)
        status = res['provisioningState']
        time.sleep(5)
    return status

def generate_options_for_cb_environment(group=None, **kwargs):
    """
    List all Azure environments that are orderable by the current group.
    """
    envs = Environment.objects.filter(
        resource_handler__resource_technology__name='Azure') \
        .select_related('resource_handler')
    if group:
        group_env_ids = [env.id for env in group.get_available_environments()]
        envs = envs.filter(id__in=group_env_ids)
    return [
        (env.id, u'{env} ({region})'.format(
            env=env, region=env.resource_handler.cast()))
        for env in envs
    ]


def create_required_parameters():
    CustomField.objects.get_or_create(
        name='create_aks_k8s_cluster_env',
        defaults=dict(
            label="AKS Cluster: Environment",
            description="Used by the AKS Cluster blueprint",
            type="INT"
        ))
    CustomField.objects.get_or_create(
        name='create_aks_k8s_cluster_name',
        defaults=dict(
            label="AKS Cluster: Cluster Name",
            description="Used by the AKS Cluster blueprint",
            type="STR"
        ))
    CustomField.objects.get_or_create(
        name='create_aks_k8s_cluster_id',
        defaults=dict(
            label="AKS Cluster: Cluster ID",
            description="Used by the AKS Cluster blueprint",
            type="INT",
        ))


def run(job=None, logger=None, **kwargs):
    """
    Create a cluster, poll until the IP address becomes available, and import
    the cluster into CB.
    """

    # Save cluster data on the resource so teardown works later
    create_required_parameters()
    resource = kwargs['resource']
    resource.create_aks_k8s_cluster_env = environment.id
    resource.create_aks_k8s_cluster_name = CLUSTER_NAME
    resource.name = CLUSTER_NAME
    resource.save()

    get_credentials()
    get_service_profile()
    get_resource_client()
    get_container_client()
    get_service_profile()

    job.set_progress("Creating Resource Group {}".format(resource_group))
    create_resource_group()

    job.set_progress("Creating Cluster {}".format(CLUSTER_NAME))
    create_cluster(NODE_COUNT)

    start = time.time()
    remaining_time = TIMEOUT - (time.time() - start)

    status = wait_for_running_status(timeout=remaining_time)
    job.set_progress('Waiting up to {} seconds for provisioning to complete.'
                         .format(remaining_time))
    job.set_progress("Configuring kubectl to connect to kubernetes cluster")

    # configure kubectl to connect to kubernetes cluster
    subprocess.run(['az', 'aks', 'get-credentials', '-g', resource_group, '-n', CLUSTER_NAME])
    start = time.time()

    config.load_kube_config()

    job.set_progress("Creating pod template container")
    api_instance = client.ExtensionsV1beta1Api()

    deployment = create_deployment_object()

    job.set_progress("Creating Deployment")
    create_deployment(api_instance, deployment)

    job.set_progress("Creating Service {}".format(service))
    create_service()

    job.set_progress("Waiting for cluster IP address...")
    endpoint = wait_for_endpoint(timeout=TIMEOUT)
    if not endpoint:
        return ("FAILURE", "No IP address returned after {} seconds".format(TIMEOUT),
                "")
    remaining_time = TIMEOUT - (time.time() - start)
    job.set_progress('Waiting for nodes to report hostnames...')

    nodes = wait_for_nodes(NODE_COUNT, timeout=remaining_time)
    if len(nodes) < NODE_COUNT:
        return ("FAILURE",
                "Nodes are not ready after {} seconds".format(TIMEOUT),
                "")

    job.set_progress('Importing cluster...')

    get_cluster()
    tech = ContainerOrchestratorTechnology.objects.get(name='Kubernetes')
    kubernetes = Kubernetes.objects.create(
        name=CLUSTER_NAME,
        ip=get_cluster_endpoint(),
        port=443,
        protocol='https',
        serviceaccount=handler.serviceaccount,
        servicepasswd=handler.secret,
        container_technology=tech,
    )

    resource.create_aks_k8s_cluster_id = kubernetes.id
    resource.save()
    url = 'https://{}{}'.format(
        PortalConfig.get_current_portal().domain,
        reverse('container_orchestrator_detail', args=[kubernetes.id])
    )
    job.set_progress("Cluster URL: {}".format(url))

    job.set_progress('Importing nodes...')

    job.set_progress('Waiting for cluster to report as running...')
    remaining_time = TIMEOUT - (time.time() - start)

    return ("SUCCESS","", "")
