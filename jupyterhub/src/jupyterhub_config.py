# This file provides common configuration for the different ways that
# the deployment can run. Configuration specific to the different modes
# will be read from separate files at the end of this configuration
# file.

import os
import json
import string
import yaml

import requests
import wrapt

from tornado import gen

from kubernetes.client.rest import ApiException

from kubernetes.client.configuration import Configuration
from kubernetes.config.incluster_config import load_incluster_config
from kubernetes.client.api_client import ApiClient
from openshift.dynamic import DynamicClient

# The application name and configuration type are passed in through the
# template. The application name should be the value used for the
# deployment, and more specifically, must match the name of the route.
# The configuration type will vary based on the template, as the setup
# required for each will be different.

application_name = os.environ.get('APPLICATION_NAME', 'homeroom')

configuration_type = os.environ.get('CONFIGURATION_TYPE', 'hosted-workshop')

# Work out the service account name and name of the namespace that the
# deployment is in.

service_account_name = '%s-hub' % application_name

service_account_path = '/var/run/secrets/kubernetes.io/serviceaccount'

with open(os.path.join(service_account_path, 'namespace')) as fp:
    namespace = fp.read().strip()

# Determine the Kubernetes REST API endpoint and cluster information,
# including working out the address of the internal image regstry.

kubernetes_service_host = os.environ['KUBERNETES_SERVICE_HOST']
kubernetes_service_port = os.environ['KUBERNETES_SERVICE_PORT']

kubernetes_server_url = 'https://%s:%s' % (kubernetes_service_host,
        kubernetes_service_port)

kubernetes_server_version_url = '%s/version' % kubernetes_server_url

with requests.Session() as session:
    response = session.get(kubernetes_server_version_url, verify=False)
    kubernetes_server_info = json.loads(response.content.decode('UTF-8'))

image_registry = 'image-registry.openshift-image-registry.svc:5000'

if kubernetes_server_info['major'] == '1':
    if kubernetes_server_info['minor'] in ('10', '10+', '11', '11+'):
        image_registry = 'docker-registry.default.svc:5000'

# Initialise the client for the REST API used doing configuration.
#
# XXX Currently have a workaround here for OpenShift 4.0 beta versions
# which disables verification of the certificate. If don't use this the
# Python openshift/kubernetes clients will fail. We also disable any
# warnings from urllib3 to get rid of the noise in the logs this creates.

load_incluster_config()

import urllib3
urllib3.disable_warnings()
instance = Configuration()
instance.verify_ssl = False
Configuration.set_default(instance)

api_client = DynamicClient(ApiClient())

image_stream_resource = api_client.resources.get(
     api_version='image.openshift.io/v1', kind='ImageStream')

route_resource = api_client.resources.get(
     api_version='route.openshift.io/v1', kind='Route')

# Workaround bug in minishift where a service cannot be contacted from a
# pod which backs the service. For further details see the minishift issue
# https://github.com/minishift/minishift/issues/2400.
#
# What these workarounds do is monkey patch the JupyterHub proxy client
# API code, and the code for creating the environment for local service
# processes, and when it sees something which uses the service name as
# the target in a URL, it replaces it with localhost. These work because
# the proxy/service processes are in the same pod. It is not possible to
# change hub_connect_ip to localhost because that is passed to other
# pods which need to contact back to JupyterHub, and so it must be left
# as the service name.

@wrapt.patch_function_wrapper('jupyterhub.proxy', 'ConfigurableHTTPProxy.add_route')
def _wrapper_add_route(wrapped, instance, args, kwargs):
    def _extract_args(routespec, target, data, *_args, **_kwargs):
        return (routespec, target, data, _args, _kwargs)

    routespec, target, data, _args, _kwargs = _extract_args(*args, **kwargs)

    old = 'http://%s:%s' % (c.JupyterHub.hub_connect_ip, c.JupyterHub.hub_port)
    new = 'http://127.0.0.1:%s' % c.JupyterHub.hub_port

    if target.startswith(old):
        target = target.replace(old, new)

    return wrapped(routespec, target, data, *_args, **_kwargs)

@wrapt.patch_function_wrapper('jupyterhub.spawner', 'LocalProcessSpawner.get_env')
def _wrapper_get_env(wrapped, instance, args, kwargs):
    env = wrapped(*args, **kwargs)

    target = env.get('JUPYTERHUB_API_URL')

    old = 'http://%s:%s' % (c.JupyterHub.hub_connect_ip, c.JupyterHub.hub_port)
    new = 'http://127.0.0.1:%s' % c.JupyterHub.hub_port

    if target and target.startswith(old):
        target = target.replace(old, new)
        env['JUPYTERHUB_API_URL'] = target

    return env

# Define all the defaults for for JupyterHub instance for our setup.

c.JupyterHub.port = 8080

c.JupyterHub.hub_ip = '0.0.0.0'
c.JupyterHub.hub_port = 8081

c.JupyterHub.hub_connect_ip = application_name

c.ConfigurableHTTPProxy.api_url = 'http://127.0.0.1:8082'

c.Spawner.start_timeout = 180
c.Spawner.http_timeout = 60

c.KubeSpawner.port = 10080

c.KubeSpawner.common_labels = {
    'app': '%s-%s' % (application_name, namespace)
}

c.KubeSpawner.extra_labels = {
    'spawner': configuration_type,
    'class': 'session',
    'user': '{username}'
}

c.KubeSpawner.uid = os.getuid()
c.KubeSpawner.fs_gid = os.getuid()

c.KubeSpawner.extra_annotations = {
    "alpha.image.policy.openshift.io/resolve-names": "*"
}

c.KubeSpawner.cmd = ['start-singleuser.sh']

c.KubeSpawner.pod_name_template = '%s-%s-{username}' % (
        application_name, namespace)

c.JupyterHub.admin_access = False

if os.environ.get('JUPYTERHUB_COOKIE_SECRET'):
    c.JupyterHub.cookie_secret = os.environ[
            'JUPYTERHUB_COOKIE_SECRET'].encode('UTF-8')
else:
    c.JupyterHub.cookie_secret_file = '/opt/app-root/data/cookie_secret'

c.JupyterHub.db_url = '/opt/app-root/data/database.sqlite'

c.JupyterHub.authenticator_class = 'tmpauthenticator.TmpAuthenticator'

c.JupyterHub.spawner_class = 'kubespawner.KubeSpawner'

c.JupyterHub.logo_file = '/opt/app-root/src/images/HomeroomIcon.png'

c.Spawner.environment = dict()

c.JupyterHub.services = []

c.KubeSpawner.init_containers = []

c.KubeSpawner.extra_containers = []

c.JupyterHub.extra_handlers = []

# Determine amount of memory to allocate for workshop environment.

def convert_size_to_bytes(size):
    multipliers = {
        'k': 1000,
        'm': 1000**2,
        'g': 1000**3,
        't': 1000**4,
        'ki': 1024,
        'mi': 1024**2,
        'gi': 1024**3,
        'ti': 1024**4,
    }

    size = str(size)

    for suffix in multipliers:
        if size.lower().endswith(suffix):
            return int(size[0:-len(suffix)]) * multipliers[suffix]
    else:
        if size.lower().endswith('b'):
            return int(size[0:-1])

    try:
        return int(size)
    except ValueError:
        raise RuntimeError('"%s" is not a valid memory specification. Must be an integer or a string with suffix K, M, G, T, Ki, Mi, Gi or Ti.' % size)

c.Spawner.mem_limit = convert_size_to_bytes(
        os.environ.get('WORKSHOP_MEMORY', '512Mi'))

# Override the image details with that for the terminal or dashboard
# image being used. The default is to assume that a image stream with
# the same name as the application name is being used. The call to the
# function resolve_image_name() is to try and resolve to image registry
# when using image stream. This is to workaround issue that many
# clusters do not have image policy controller configured correctly.
#
# Note that we set the policy that images will always be pulled to the
# node each time when the image name is not explicitly provided. This is
# so that during development, changes to the terminal image will always
# be picked up. Someone developing a new image need only update the
# 'latest' tag on the image using 'oc tag'. 

terminal_image = os.environ.get('TERMINAL_IMAGE')

if not terminal_image:
    c.KubeSpawner.image_pull_policy = 'Always'
    terminal_image = '%s:latest' % application_name

def resolve_image_name(name):
    # If the image name contains a slash, we assume it is already
    # referring to an image on some image registry. Even if it does
    # not contain a slash, it may still be hosted on docker.io.

    if name.find('/') != -1:
        return name

    # Separate actual source image name and tag for the image from the
    # name. If the tag is not supplied, default to 'latest'.

    parts = name.split(':', 1)

    if len(parts) == 1:
        source_image, tag = parts, 'latest'
    else:
        source_image, tag = parts

    # See if there is an image stream in the current project with the
    # target name.

    try:
        image_stream = image_stream_resource.get(namespace=namespace,
                name=source_image)

    except ApiException as e:
        if e.status not in (403, 404):
            raise

        return name

    # If we get here then the image stream exists with the target name.
    # We need to determine if the tag exists. If it does exist, we
    # extract out the full name of the image including the reference
    # to the image registry it is hosted on.

    if image_stream.status.tags:
        for entry in image_stream.status.tags:
            if entry.tag == tag:
                registry_image = image_stream.status.dockerImageRepository
                if registry_image:
                    return '%s:%s' % (registry_image, tag)

    # Use original value if can't find a matching tag.

    return name

c.KubeSpawner.image = resolve_image_name(terminal_image)

# Work out hostname for the exposed route of the JupyterHub server. This
# is tricky as we need to use the REST API to query it. We assume that
# a secure route is always used. This is used when needing to do OAuth.

routes = route_resource.get(namespace=namespace)

def extract_hostname(routes, name):
    for route in routes.items:
        if route.metadata.name == name:
            return route.spec.host

public_hostname = extract_hostname(routes, application_name)

if not public_hostname:
    raise RuntimeError('Cannot calculate external host name for JupyterHub.')

c.Spawner.environment['JUPYTERHUB_ROUTE'] = 'https://%s' % public_hostname

# Work out the subdomain under which applications hosted in the cluster
# are hosted. Calculate this from the route for the JupyterHub route if
# not supplied explicitly.

cluster_subdomain = os.environ.get('CLUSTER_SUBDOMAIN')

if not cluster_subdomain:
    cluster_subdomain = '.'.join(public_hostname.split('.')[1:])

c.Spawner.environment['CLUSTER_SUBDOMAIN'] = cluster_subdomain

# The terminal image will normally work out what versions of OpenShift
# and Kubernetes command line tools should be used, based on the version
# of OpenShift which is being used. Allow these to be overridden if
# necessary.

if os.environ.get('OC_VERSION'):
    c.Spawner.environment['OC_VERSION'] = os.environ.get('OC_VERSION')
if os.environ.get('ODO_VERSION'):
    c.Spawner.environment['ODO_VERSION'] = os.environ.get('ODO_VERSION')
if os.environ.get('KUBECTL_VERSION'):
    c.Spawner.environment['KUBECTL_VERSION'] = os.environ.get('KUBECTL_VERSION')

# Common functions for creating projects, injecting resources etc.

project_resource = api_client.resources.get(
     api_version='project.openshift.io/v1', kind='Project')

namespace_resource = api_client.resources.get(
     api_version='v1', kind='Namespace')

service_account_resource = api_client.resources.get(
     api_version='v1', kind='ServiceAccount')

secret_resource = api_client.resources.get(
     api_version='v1', kind='Secret')

cluster_role_resource = api_client.resources.get(
     api_version='rbac.authorization.k8s.io/v1', kind='ClusterRole')

role_binding_resource = api_client.resources.get(
     api_version='rbac.authorization.k8s.io/v1', kind='RoleBinding')

limit_range_resource = api_client.resources.get(
     api_version='v1', kind='LimitRange')

resource_quota_resource = api_client.resources.get(
     api_version='v1', kind='ResourceQuota')

service_resource = api_client.resources.get(
     api_version='v1', kind='Service')

route_resource = api_client.resources.get(
     api_version='route.openshift.io/v1', kind='Route')

namespace_template = string.Template("""
{
    "kind": "Namespace",
    "apiVersion": "v1",
    "metadata": {
        "name": "${name}",
        "labels": {
            "app": "${hub}",
            "spawner": "${configuration}",
            "class": "session",
            "user": "${username}"
        },
        "annotations": {
            "spawner/requestor": "${requestor}",
            "spawner/namespace": "${namespace}",
            "spawner/deployment": "${deployment}",
            "spawner/account": "${account}",
            "spawner/session": "${session}"
        },
        "ownerReferences": [
            {
                "apiVersion": "v1",
                "kind": "ClusterRole",
                "blockOwnerDeletion": false,
                "controller": true,
                "name": "${owner}",
                "uid": "${uid}"
            }
        ]
    }
}
""")

service_account_template = string.Template("""
{
    "kind": "ServiceAccount",
    "apiVersion": "v1",
    "metadata": {
        "name": "${name}",
        "labels": {
            "app": "${hub}",
            "spawner": "${configuration}",
            "class": "session",
            "user": "${username}"
        }
    }
}
""")

role_binding_template = string.Template("""
{
    "kind": "RoleBinding",
    "apiVersion": "rbac.authorization.k8s.io/v1",
    "metadata": {
        "name": "${name}-${tag}",
        "labels": {
            "app": "${hub}",
            "spawner": "${configuration}",
            "class": "session",
            "user": "${username}"
        }
    },
    "subjects": [
        {
            "kind": "ServiceAccount",
            "namespace": "${namespace}",
            "name": "${name}"
        }
    ],
    "roleRef": {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "${role}"
    }
}
""")

resource_budget_mapping = {
    "small": {
        "resource-limits" : {
            "kind": "LimitRange",
            "apiVersion": "v1",
            "metadata": {
                "name": "resource-limits",
                "annotations": {
                    "resource-budget": "small"
                }
            },
            "spec": {
                "limits": [
                    {
                        "type": "Pod",
                        "min": {
                            "cpu": "50m",
                            "memory": "32Mi"
                        },
                        "max": {
                            "cpu": "1",
                            "memory": "1Gi"
                        }
                    },
                    {
                        "type": "Container",
                        "min": {
                            "cpu": "50m",
                            "memory": "32Mi"
                        },
                        "max": {
                            "cpu": "1",
                            "memory": "1Gi"
                        },
                        "default": {
                            "cpu": "250m",
                            "memory": "256Mi"
                        },
                        "defaultRequest": {
                            "cpu": "50m",
                            "memory": "128Mi"
                        }
                    },
                    {
                        "type": "PersistentVolumeClaim",
                        "min": {
                            "storage": "1Gi"
                        },
                        "max": {
                            "storage": "1Gi"
                        }
                    }
                ]
            }
        },
        "compute-resources" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "compute-resources",
                "annotations": {
                    "resource-budget": "small"
                }
            },
            "spec": {
                "hard": {
                    "limits.cpu": "1",
                    "limits.memory": "1Gi"
                },
                "scopes": [
                    "NotTerminating"
                ]
            }
        },
        "compute-resources-timebound" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "compute-resources-timebound",
                "annotations": {
                    "resource-budget": "small"
                }
            },
            "spec": {
                "hard": {
                    "limits.cpu": "1",
                    "limits.memory": "1Gi"
                },
                "scopes": [
                    "Terminating"
                ]
            }
        },
        "object-counts" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "object-counts",
                "annotations": {
                    "resource-budget": "small"
                }
            },
            "spec": {
                "hard": {
                    "persistentvolumeclaims": "3",
                    "replicationcontrollers": "10",
                    "secrets": "20",
                    "services": "5"
                }
            }
        },
    },
    "medium": {
        "resource-limits" : {
            "kind": "LimitRange",
            "apiVersion": "v1",
            "metadata": {
                "name": "resource-limits",
                "annotations": {
                    "resource-budget": "medium"
                }
            },
            "spec": {
                "limits": [
                    {
                        "type": "Pod",
                        "min": {
                            "cpu": "50m",
                            "memory": "32Mi"
                        },
                        "max": {
                            "cpu": "2",
                            "memory": "2Gi"
                        }
                    },
                    {
                        "type": "Container",
                        "min": {
                            "cpu": "50m",
                            "memory": "32Mi"
                        },
                        "max": {
                            "cpu": "2",
                            "memory": "2Gi"
                        },
                        "default": {
                            "cpu": "500m",
                            "memory": "512Mi"
                        },
                        "defaultRequest": {
                            "cpu": "50m",
                            "memory": "128Mi"
                        }
                    },
                    {
                        "type": "PersistentVolumeClaim",
                        "min": {
                            "storage": "1Gi"
                        },
                        "max": {
                            "storage": "5Gi"
                        }
                    }
                ]
            }
        },
        "compute-resources" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "compute-resources",
                "annotations": {
                    "resource-budget": "medium"
                }
            },
            "spec": {
                "hard": {
                    "limits.cpu": "2",
                    "limits.memory": "2Gi"
                },
                "scopes": [
                    "NotTerminating"
                ]
            }
        },
        "compute-resources-timebound" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "compute-resources-timebound",
                "annotations": {
                    "resource-budget": "medium"
                }
            },
            "spec": {
                "hard": {
                    "limits.cpu": "2",
                    "limits.memory": "2Gi"
                },
                "scopes": [
                    "Terminating"
                ]
            }
        },
        "object-counts" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "object-counts",
                "annotations": {
                    "resource-budget": "medium"
                }
            },
            "spec": {
                "hard": {
                    "persistentvolumeclaims": "6",
                    "replicationcontrollers": "15",
                    "secrets": "25",
                    "services": "10"
                }
            }
        },
    },
    "large": {
        "resource-limits" : {
            "kind": "LimitRange",
            "apiVersion": "v1",
            "metadata": {
                "name": "resource-limits",
                "annotations": {
                    "resource-budget": "large"
                }
            },
            "spec": {
                "limits": [
                    {
                        "type": "Pod",
                        "min": {
                            "cpu": "50m",
                            "memory": "32Mi"
                        },
                        "max": {
                            "cpu": "4",
                            "memory": "4Gi"
                        }
                    },
                    {
                        "type": "Container",
                        "min": {
                            "cpu": "50m",
                            "memory": "32Mi"
                        },
                        "max": {
                            "cpu": "4",
                            "memory": "4Gi"
                        },
                        "default": {
                            "cpu": "500m",
                            "memory": "1Gi"
                        },
                        "defaultRequest": {
                            "cpu": "50m",
                            "memory": "128Mi"
                        }
                    },
                    {
                        "type": "PersistentVolumeClaim",
                        "min": {
                            "storage": "1Gi"
                        },
                        "max": {
                            "storage": "10Gi"
                        }
                    }
                ]
            }
        },
        "compute-resources" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "compute-resources",
                "annotations": {
                    "resource-budget": "large"
                }
            },
            "spec": {
                "hard": {
                    "limits.cpu": "4",
                    "limits.memory": "4Gi"
                },
                "scopes": [
                    "NotTerminating"
                ]
            }
        },
        "compute-resources-timebound" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "compute-resources-timebound",
                "annotations": {
                    "resource-budget": "large"
                }
            },
            "spec": {
                "hard": {
                    "limits.cpu": "4",
                    "limits.memory": "4Gi"
                },
                "scopes": [
                    "Terminating"
                ]
            }
        },
        "object-counts" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "object-counts",
                "annotations": {
                    "resource-budget": "large"
                }
            },
            "spec": {
                "hard": {
                    "persistentvolumeclaims": "12",
                    "replicationcontrollers": "25",
                    "secrets": "35",
                    "services": "20"
                }
            }
        }
    },
    "x-large": {
        "resource-limits" : {
            "kind": "LimitRange",
            "apiVersion": "v1",
            "metadata": {
                "name": "resource-limits",
                "annotations": {
                    "resource-budget": "x-large"
                }
            },
            "spec": {
                "limits": [
                    {
                        "type": "Pod",
                        "min": {
                            "cpu": "50m",
                            "memory": "32Mi"
                        },
                        "max": {
                            "cpu": "8",
                            "memory": "8Gi"
                        }
                    },
                    {
                        "type": "Container",
                        "min": {
                            "cpu": "50m",
                            "memory": "32Mi"
                        },
                        "max": {
                            "cpu": "8",
                            "memory": "8Gi"
                        },
                        "default": {
                            "cpu": "500m",
                            "memory": "2Gi"
                        },
                        "defaultRequest": {
                            "cpu": "50m",
                            "memory": "128Mi"
                        }
                    },
                    {
                        "type": "PersistentVolumeClaim",
                        "min": {
                            "storage": "1Gi"
                        },
                        "max": {
                            "storage": "20Gi"
                        }
                    }
                ]
            }
        },
        "compute-resources" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "compute-resources",
                "annotations": {
                    "resource-budget": "x-large"
                }
            },
            "spec": {
                "hard": {
                    "limits.cpu": "8",
                    "limits.memory": "8Gi"
                },
                "scopes": [
                    "NotTerminating"
                ]
            }
        },
        "compute-resources-timebound" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "compute-resources-timebound",
                "annotations": {
                    "resource-budget": "x-large"
                }
            },
            "spec": {
                "hard": {
                    "limits.cpu": "8",
                    "limits.memory": "8Gi"
                },
                "scopes": [
                    "Terminating"
                ]
            }
        },
        "object-counts" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "object-counts",
                "annotations": {
                    "resource-budget": "x-large"
                }
            },
            "spec": {
                "hard": {
                    "persistentvolumeclaims": "18",
                    "replicationcontrollers": "35",
                    "secrets": "45",
                    "services": "30"
                }
            }
        }
    },
    "xx-large": {
        "resource-limits" : {
            "kind": "LimitRange",
            "apiVersion": "v1",
            "metadata": {
                "name": "resource-limits",
                "annotations": {
                    "resource-budget": "xx-large"
                }
            },
            "spec": {
                "limits": [
                    {
                        "type": "Pod",
                        "min": {
                            "cpu": "50m",
                            "memory": "32Mi"
                        },
                        "max": {
                            "cpu": "12",
                            "memory": "12Gi"
                        }
                    },
                    {
                        "type": "Container",
                        "min": {
                            "cpu": "50m",
                            "memory": "32Mi"
                        },
                        "max": {
                            "cpu": "12",
                            "memory": "12Gi"
                        },
                        "default": {
                            "cpu": "500m",
                            "memory": "2Gi"
                        },
                        "defaultRequest": {
                            "cpu": "50m",
                            "memory": "128Mi"
                        }
                    },
                    {
                        "type": "PersistentVolumeClaim",
                        "min": {
                            "storage": "1Gi"
                        },
                        "max": {
                            "storage": "20Gi"
                        }
                    }
                ]
            }
        },
        "compute-resources" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "compute-resources",
                "annotations": {
                    "resource-budget": "xx-large"
                }
            },
            "spec": {
                "hard": {
                    "limits.cpu": "12",
                    "limits.memory": "12Gi"
                },
                "scopes": [
                    "NotTerminating"
                ]
            }
        },
        "compute-resources-timebound" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "compute-resources-timebound",
                "annotations": {
                    "resource-budget": "xx-large"
                }
            },
            "spec": {
                "hard": {
                    "limits.cpu": "12",
                    "limits.memory": "12Gi"
                },
                "scopes": [
                    "Terminating"
                ]
            }
        },
        "object-counts" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "object-counts",
                "annotations": {
                    "resource-budget": "xx-large"
                }
            },
            "spec": {
                "hard": {
                    "persistentvolumeclaims": "24",
                    "replicationcontrollers": "45",
                    "secrets": "55",
                    "services": "40"
                }
            }
        }
    },
    "xxx-large": {
        "resource-limits" : {
            "kind": "LimitRange",
            "apiVersion": "v1",
            "metadata": {
                "name": "resource-limits",
                "annotations": {
                    "resource-budget": "xxx-large"
                }
            },
            "spec": {
                "limits": [
                    {
                        "type": "Pod",
                        "min": {
                            "cpu": "50m",
                            "memory": "32Mi"
                        },
                        "max": {
                            "cpu": "16",
                            "memory": "16Gi"
                        }
                    },
                    {
                        "type": "Container",
                        "min": {
                            "cpu": "50m",
                            "memory": "32Mi"
                        },
                        "max": {
                            "cpu": "16",
                            "memory": "16Gi"
                        },
                        "default": {
                            "cpu": "500m",
                            "memory": "2Gi"
                        },
                        "defaultRequest": {
                            "cpu": "50m",
                            "memory": "128Mi"
                        }
                    },
                    {
                        "type": "PersistentVolumeClaim",
                        "min": {
                            "storage": "1Gi"
                        },
                        "max": {
                            "storage": "20Gi"
                        }
                    }
                ]
            }
        },
        "compute-resources" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "compute-resources",
                "annotations": {
                    "resource-budget": "xxx-large"
                }
            },
            "spec": {
                "hard": {
                    "limits.cpu": "16",
                    "limits.memory": "16Gi"
                },
                "scopes": [
                    "NotTerminating"
                ]
            }
        },
        "compute-resources-timebound" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "compute-resources-timebound",
                "annotations": {
                    "resource-budget": "xxx-large"
                }
            },
            "spec": {
                "hard": {
                    "limits.cpu": "16",
                    "limits.memory": "16Gi"
                },
                "scopes": [
                    "Terminating"
                ]
            }
        },
        "object-counts" : {
            "kind": "ResourceQuota",
            "apiVersion": "v1",
            "metadata": {
                "name": "object-counts",
                "annotations": {
                    "resource-budget": "xxx-large"
                }
            },
            "spec": {
                "hard": {
                    "persistentvolumeclaims": "30",
                    "replicationcontrollers": "55",
                    "secrets": "65",
                    "services": "50"
                }
            }
        }
    }
}

service_template = string.Template("""
{
    "kind": "Service",
    "apiVersion": "v1",
    "metadata": {
        "name": "${name}",
        "labels": {
            "app": "${hub}",
            "spawner": "${configuration}",
            "class": "session",
            "user": "${username}"
        },
        "ownerReferences": [
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "blockOwnerDeletion": false,
                "controller": true,
                "name": "${name}",
                "uid": "${uid}"
            }
        ]
    },
    "spec": {
        "type": "ClusterIP",
        "selector": {
            "app": "${hub}",
            "spawner": "${configuration}",
            "user": "${username}"
        },
        "ports": []
    }
}
""")

route_template = string.Template("""
{
    "apiVersion": "route.openshift.io/v1",
    "kind": "Route",
    "metadata": {
        "name": "${name}-${port}",
        "labels": {
            "app": "${hub}",
            "spawner": "${configuration}",
            "class": "session",
            "user": "${username}",
            "port": "${port}"
        },
        "ownerReferences": [
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "blockOwnerDeletion": false,
                "controller": true,
                "name": "${name}",
                "uid": "${uid}"
            }
        ]
    },
    "spec": {
        "host": "${host}",
        "port": {
            "targetPort": "${port}-tcp"
        },
        "to": {
            "kind": "Service",
            "name": "${name}",
            "weight": 100
        }
    }
}
""")

@gen.coroutine
def setup_project_namespace(spawner, project_name, role, budget):
    # Wait for project to exist before continuing.

    for _ in range(30):
        try:
            project = project_resource.get(name=project_name)

        except ApiException as e:
            if e.status == 404:
                yield gen.sleep(0.1)
                continue

            print('ERROR: Error querying project. %s' % e)
            raise

        else:
            break

    else:
        # If can't verify project created, carry on anyway.

        print('ERROR: Could not verify project creation. %s' % project_name)

        raise Exception('Could not verify project creation. %s' % project_name)

    # Create role binding in the project so the hub service account
    # can delete project when done. Will fail if the project hasn't
    # actually been created yet.

    hub = '%s-%s' % (application_name, namespace)
    short_name = spawner.user.name
    user_account_name = '%s-%s' % (hub, short_name)
    hub_account_name = '%s-hub' % hub

    try:
        text = role_binding_template.safe_substitute(
                configuration=configuration_type, namespace=namespace,
                name=user_account_name, tag=role, role=role, hub=hub,
                username=short_name)
        body = json.loads(text)

        role_binding_resource.create(namespace=project_name, body=body)

    except ApiException as e:
        if e.status != 409:
            print('ERROR: Error creating role binding for hub. %s' % e)
            raise

    except Exception as e:
        print('ERROR: Error creating rolebinding for hub. %s' % e)
        raise

extra_resources = {}
extra_resources_loader = None

if os.path.exists('/opt/app-root/resources/extra_resources.yaml'):
    with open('/opt/app-root/resources/extra_resources.yaml') as fp:
        extra_resources = fp.read().strip()
        extra_resources_loader = yaml.safe_load

if os.path.exists('/opt/app-root/resources/extra_resources.json'):
    with open('/opt/app-root/resources/extra_resources.json') as fp:
        extra_resources = fp.read().strip()
        extra_resources_loader = json.loads

@gen.coroutine
def create_extra_resources(spawner, pod, project_name, project_uid,
        user_account_name, short_name):

    if not extra_resources:
        return

    # Only passing 'jupyterhub_namespace' for backward compatibility.
    # Should use 'spawner_namespace' going forward.

    template = string.Template(extra_resources)
    text = template.safe_substitute(jupyterhub_namespace=namespace,
            spawner_namespace=namespace, project_namespace=project_name,
            image_registry=image_registry, service_account=user_account_name,
            username=short_name, application_name=application_name)

    data = extra_resources_loader(text)

    if isinstance(data, dict) and data.get('kind') == 'List':
        data = data['items']

    for body in data:
        try:
            kind = body['kind']
            api_version = body['apiVersion']

            if kind.lower() in ('securitycontextconstraints',
                    'clusterrolebinding', 'namespace'):
                body['metadata']['ownerReferences'] = [dict(
                    apiVersion='v1', kind='Namespace', blockOwnerDeletion=False,
                    controller=True, name=project_name, uid=project_uid)]

            if kind.lower() == 'namespace':
                service_account_name = 'system:serviceaccount:%s:%s-%s-hub' % (
                        namespace, application_name, namespace)

                annotations = body['metadata'].setdefault('annotations', {})

                annotations['spawner/requestor'] = service_account_name
                annotations['spawner/namespace'] = namespace
                annotations['spawner/deployment'] = application_name
                annotations['spawner/account'] = user_account_name
                annotations['spawner/session'] = pod.metadata.name

            resource = api_client.resources.get(api_version=api_version, kind=kind)

            target_namespace = body['metadata'].get('namespace', project_name)

            resource.create(namespace=target_namespace, body=body)

        except ApiException as e:
            if e.status != 409:
                print('ERROR: Error creating resource %s. %s' % (body, e))
                raise

            else:
                print('WARNING: Resource already exists %s.' % body)

        except Exception as e:
            print('ERROR: Error creating resource %s. %s' % (body, e))
            raise

        if kind.lower() == 'namespace':
            annotations = body['metadata'].get('annotations', {})
            role = annotations.get('session/role', 'admin')

            default_budget = os.environ.get('RESOURCE_BUDGET', 'default')
            budget = annotations.get('session/budget', default_budget)

            yield setup_project_namespace(spawner, body['metadata']['name'],
                    role, budget)

# Load configuration corresponding to the deployment mode.

c.Spawner.environment['DEPLOYMENT_TYPE'] = 'spawner'
c.Spawner.environment['CONFIGURATION_TYPE'] = configuration_type

config_root = '/opt/app-root/src/configs'
config_file = '%s/%s.py' % (config_root, configuration_type)

if os.path.exists(config_file):
    with open(config_file) as fp:
        exec(compile(fp.read(), config_file, 'exec'), globals())

# Load configuration provided via the environment.

environ_config_file = '/opt/app-root/configs/jupyterhub_config.py'

if os.path.exists(environ_config_file):
    with open(environ_config_file) as fp:
        exec(compile(fp.read(), environ_config_file, 'exec'), globals())
