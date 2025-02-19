import abc
from openshift_nightlies.util import var_loader, executor, constants
from openshift_nightlies.tasks.index.status import StatusIndexer
from openshift_nightlies.models.release import OpenshiftRelease
from openshift_nightlies.models.dag_config import DagConfig

from os import environ
import json
import requests
from abc import ABC, abstractmethod

from airflow.operators.bash import BashOperator
from airflow.models import Variable
from kubernetes.client import models as k8s

class AbstractOpenshiftInstaller(ABC):
    def __init__(self, dag, config: DagConfig, release: OpenshiftRelease):

        # General DAG Configuration
        self.dag = dag
        self.release = release
        self.dag_config = config
        self.release_name = release.get_release_name(delimiter="-")

        # Specific Task Configuration
        self.vars = var_loader.build_task_vars(
            release, task="install")

        # Airflow Variables
        self.ansible_orchestrator = var_loader.get_secret(
            "ansible_orchestrator", deserialize_json=True)

        self.install_secrets = var_loader.get_secret(
            f"openshift_install_config", deserialize_json=True)
        self.aws_creds = var_loader.get_secret("aws_creds", deserialize_json=True)
        self.gcp_creds = var_loader.get_secret("gcp_creds", deserialize_json=True)
        self.azure_creds = var_loader.get_secret("azure_creds", deserialize_json=True)
        self.ocp_pull_secret = var_loader.get_secret("osp_ocp_pull_creds")
        self.openstack_creds = var_loader.get_secret("openstack_creds", deserialize_json=True)
        self.rosa_creds = var_loader.get_secret("rosa_creds", deserialize_json=True)
        self.release_stream_base_url = var_loader.get_secret("release_stream_base_url")
        self.exec_config = executor.get_default_executor_config(self.dag_config)

        # Merge all variables, prioritizing Airflow Secrets over git based vars
        self.config = {
            **self.vars,
            **self.ansible_orchestrator,
            **self.install_secrets,
            **self.aws_creds,
            **self.gcp_creds,
            **self.azure_creds,
            **self.openstack_creds,
            **self.rosa_creds,
            **self.release.get_latest_release(self.release_stream_base_url),
            **{ "es_server": var_loader.get_secret('elasticsearch'),
                "thanos_receiver_url": var_loader.get_secret('thanos_receiver_url'),
                "loki_receiver_url": var_loader.get_secret('loki_receiver_url') }
        }
        super().__init__()

    @abstractmethod
    def _get_task(self, operation="install", trigger_rule="all_success"):
        raise NotImplementedError()

    def get_install_task(self):
        indexer = StatusIndexer(self.dag, self.dag_config, self.release,
                                "install").get_index_task()
        install_task = self._get_task(operation="install")
        install_task >> indexer
        return install_task

    def get_cleanup_task(self):
        # trigger_rule = "all_done" means this task will run when every other task has finished, whether it fails or succeededs
        return self._get_task(operation="cleanup")

    def _setup_task(self, operation="install"):
        self.config = {**self.config,
                       ** self._get_playbook_operations(operation)}
        self.config['openshift_cluster_name'] = self._generate_cluster_name()
        self.config['dynamic_deploy_path'] = f"{self.config['openshift_cluster_name']}"
        self.config['kubeconfig_path'] = f"/root/{self.config['dynamic_deploy_path']}/auth/kubeconfig"
        self.env = {
            "SSHKEY_TOKEN": self.config['sshkey_token'],
            "ORCHESTRATION_HOST": self.config['orchestration_host'],
            "ORCHESTRATION_USER": self.config['orchestration_user'],
            "OPENSHIFT_CLUSTER_NAME": self.config['openshift_cluster_name'],
            "DEPLOY_PATH": self.config['dynamic_deploy_path'],
            "KUBECONFIG_NAME": f"{self.release_name}-kubeconfig",
            "KUBEADMIN_NAME": f"{self.release_name}-kubeadmin",
            "OPENSHIFT_INSTALL_PULL_SECRET": self.ocp_pull_secret,
            "AWS_REGION": self.config['aws_region_for_openshift'],
            **self._insert_kube_env()
        }

        # Dump all vars to json file for Ansible to pick up
        with open(f"/tmp/{self.release_name}-{operation}-task.json", 'w') as json_file:
            json.dump(self.config, json_file, sort_keys=True, indent=4)

    def _generate_cluster_name(self):
        git_user = var_loader.get_git_user()
        if git_user == 'cloud-bulldozer':
            return f"ci-{self.release_name}"
        else: 
            return f"{git_user}-{self.release_name}"

    def _get_playbook_operations(self, operation):
        if operation == "install":
            return {"openshift_cleanup": True, "openshift_debug_config": False,
                    "openshift_install": True, "openshift_post_config": True, "openshift_post_install": True}
        else:
            return {"openshift_cleanup": True, "openshift_debug_config": False,
                    "openshift_install": False, "openshift_post_config": False, "openshift_post_install": False,
                    "rhacs_enable": False}

    # This Helper Injects Airflow environment variables into the task execution runtime
    # This allows the task to interface with the Kubernetes cluster Airflow is hosted on.
    def _insert_kube_env(self):
        return {key: value for (key, value) in environ.items() if "KUBERNETES" in key}
