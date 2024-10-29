import json
import os
import re
import shutil
import tempfile

import certifi
import pytest
from kubernetes import client
from kubernetes import config
from kubernetes import utils
from kubernetes import watch

NAMESPACE = os.getenv("NAMESPACE", "rapidast--pipeline")
RAPIDAST_IMAGE = os.getenv("RAPIDAST_IMAGE", "quay.io/redhatproductsecurity/rapidast:latest")

MANIFESTS = "e2e-tests/manifests"


# monkeypatch certifi so that internal CAs are trusted
def where():
    return os.getenv("REQUESTS_CA_BUNDLE", "/etc/pki/tls/certs/ca-bundle.crt")


certifi.where = where


@pytest.fixture(name="kclient")
def fixture_kclient():
    config.load_config()
    yield client.ApiClient()


@pytest.fixture(name="kclient")
def fixture_corev1():
    yield client.CoreV1Api()


def wait_until_ready(**kwargs):
    corev1 = client.CoreV1Api()
    w = watch.Watch()
    for event in w.stream(func=corev1.list_namespaced_pod, namespace=NAMESPACE, timeout_seconds=60, **kwargs):
        if not isinstance(event, dict):  # Watch.stream() can yield non-dict types
            continue
        print(event["object"].metadata.name, event["object"].status.phase)
        if event["object"].status.phase == "Running":
            return
    raise RuntimeError("Timeout out waiting for pod matching: {kwargs}")


# simulates: $ oc logs -f <pod> | tee <file>
def tee_log(pod_name: str, filename: str):
    corev1 = client.CoreV1Api()
    w = watch.Watch()
    with open(filename, "w", encoding="utf-8") as f:
        for e in w.stream(corev1.read_namespaced_pod_log, name=pod_name, namespace=NAMESPACE):
            if not isinstance(e, str):
                continue  # Watch.stream() can yield non-string types
            f.write(e + "\n")
            print(e)


def render_manifests(input_dir, output_dir):
    shutil.copytree(input_dir, output_dir, dirs_exist_ok=True)
    print(f"rendering manifests in {output_dir}")
    for filepath in os.scandir(output_dir):
        with open(filepath, "r", encoding="utf-8") as f:
            contents = f.read()
        contents = contents.replace("${IMAGE}", RAPIDAST_IMAGE)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(contents)


class TestRapiDAST:
    @classmethod
    def setup_class(cls):
        cls.tempdir = tempfile.mkdtemp()
        render_manifests(MANIFESTS, cls.tempdir)
        print(f"testing with image: {RAPIDAST_IMAGE}")
        os.system(f"kubectl delete -f {MANIFESTS}/")
        # XXX oobtukbe does not clean up after itself
        os.system("kubectl delete Task/vulnerable")

    @classmethod
    def teardown_class(cls):
        # TODO teardown should really occur after each test, so the the
        # resource count does not grown until quota reached
        os.system(f"kubectl delete -f {MANIFESTS}/")
        # XXX oobtukbe does not clean up after itself
        os.system("kubectl delete Task/vulnerable")

    def create_from_yaml(self, kclient, path: str):
        # simple wrapper to reduce repetition
        utils.create_from_yaml(kclient, path, namespace=NAMESPACE, verbose=True)

    def test_vapi(self, kclient):
        """Test rapidast find expected number of findings in VAPI"""
        self.create_from_yaml(kclient, f"{self.tempdir}/vapi-deployment.yaml")
        self.create_from_yaml(kclient, f"{self.tempdir}/vapi-service.yaml")
        wait_until_ready(label_selector="app=vapi")

        self.create_from_yaml(kclient, f"{self.tempdir}/rapidast-vapi-configmap.yaml")
        self.create_from_yaml(kclient, f"{self.tempdir}/rapidast-vapi-pod.yaml")
        wait_until_ready(field_selector="metadata.name=rapidast-vapi")

        logfile = os.path.join(self.tempdir, "rapidast-vapi.log")
        tee_log("rapidast-vapi", logfile)

        # XXX relies on rapidast-vapi pod cat-ing the result json file after execution
        with open(logfile, "r", encoding="utf-8") as f:
            logs = f.read()
            pattern = r"^{\s*$.*$"
            matches = re.findall(pattern, logs, re.MULTILINE | re.DOTALL)
            assert matches, f"{logfile} did not contain expected json results"
            results = json.loads(matches[0])

        assert len(results["site"][0]["alerts"]) == 3

    def test_trivy(self, kclient):
        self.create_from_yaml(kclient, f"{self.tempdir}/rapidast-trivy-configmap.yaml")
        self.create_from_yaml(kclient, f"{self.tempdir}/rapidast-trivy-pod.yaml")
        wait_until_ready(field_selector="metadata.name=rapidast-trivy")

        logfile = os.path.join(self.tempdir, "rapidast-trivy.log")
        tee_log("rapidast-trivy", logfile)

        expected_line = "INFO:scanner: 'generic_trivy' completed successfully"
        with open(logfile, "r", encoding="utf-8") as f:
            logs = f.read()
            assert expected_line in logs, f"{logfile} does not contain expected line: {expected_line}"

    def test_oobtkube(self, kclient):
        self.create_from_yaml(kclient, f"{self.tempdir}/task-controller-deployment.yaml")

        self.create_from_yaml(kclient, f"{self.tempdir}/rapidast-oobtkube-configmap.yaml")
        self.create_from_yaml(kclient, f"{self.tempdir}/rapidast-oobtkube-service.yaml")
        self.create_from_yaml(kclient, f"{self.tempdir}/rapidast-oobtkube-pod.yaml")
        wait_until_ready(field_selector="metadata.name=rapidast-oobtkube")

        logfile = os.path.join(self.tempdir, "rapidast-oobtkube.log")
        tee_log("rapidast-oobtkube", logfile)

        expected_line = "RESULT: OOB REQUEST DETECTED"
        with open(logfile, "r", encoding="utf-8") as f:
            logs = f.read()
            assert expected_line in logs, f"{logfile} does not contain expected line: {expected_line}"
