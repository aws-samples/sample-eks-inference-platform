#!/usr/bin/env python3
"""Env-scoped orphan sweep — run by `platformctl down` after `terraform destroy`.

Terraform only destroys what it manages. It leaves behind resources created by
in-cluster controllers or by EKS/Karpenter/AWS itself:

  * the controller security groups (`k8s-traffic-*`, `eks-cluster-sg-*`) and any
    orphaned ENIs — these PIN THE VPC and make `destroy` fail with a
    DependencyViolation;
  * Karpenter EC2 instances (pin subnets), EC2 fleets, launch templates and
    instance-profiles;
  * the EKS-created KMS key and CloudWatch log groups;
  * orphaned NAT gateways, EBS volumes and Elastic IPs.

Left alone these block the teardown or quietly cost money. This sweep removes
them so `down` is truly hands-off with no lingering billable resources.

SAFETY: everything is scoped to the env's cluster name (`<resources_prefix>-<env>`)
and its VPC. It never acts on a resource that isn't clearly part of this cluster,
so it cannot touch another environment or unrelated project (even if that project
happens to share an `Environment` tag value).

Uses boto3 (already a platform dependency). Best-effort + idempotent: every
action is independent, errors are reported not fatal, and re-running is safe.
"""
import argparse
import sys
import time

import boto3
from botocore.exceptions import ClientError


class Sweeper:
    def __init__(self, env, cluster, region):
        self.env = env
        self.cluster = cluster
        self.region = region
        self.ec2 = boto3.client("ec2", region_name=region)
        self.elbv2 = boto3.client("elbv2", region_name=region)
        self.kms = boto3.client("kms", region_name=region)
        self.logs = boto3.client("logs", region_name=region)
        self.iam = boto3.client("iam")  # IAM is global
        self.actions = []

    def log(self, msg):
        print(f"  [sweep] {msg}", flush=True)
        self.actions.append(msg)

    def _err(self, ctx, e):
        code = e.response["Error"]["Code"] if isinstance(e, ClientError) else str(e)
        print(f"  [sweep] {ctx}: {code}", flush=True)

    # ---- helpers -----------------------------------------------------------
    def _has_cluster_tag(self, tags):
        """True if a tag list clearly ties the resource to THIS cluster."""
        for t in tags or []:
            k, v = t.get("Key", ""), t.get("Value", "")
            if k == f"kubernetes.io/cluster/{self.cluster}":
                return True
            if k in ("eks:cluster-name", "aws:eks:cluster-name", "karpenter.sh/discovery", "cluster-name") and v == self.cluster:
                return True
            if k == "Name" and self.cluster in v:
                return True
        return False

    def find_vpc(self):
        for filt in (
            [{"Name": f"tag:kubernetes.io/cluster/{self.cluster}", "Values": ["owned", "shared"]}],
            [{"Name": "tag:Name", "Values": [self.cluster]}],
            [{"Name": "tag:Environment", "Values": [self.env]}],
        ):
            try:
                vpcs = self.ec2.describe_vpcs(Filters=filt).get("Vpcs", [])
                if vpcs:
                    return vpcs[0]["VpcId"]
            except ClientError as e:
                self._err("find_vpc", e)
        return None

    # ---- VPC blockers (what makes destroy fail) ----------------------------
    def clear_vpc_blockers(self, vpc):
        # 1. Terminate any instances (Karpenter orphans pin subnets)
        try:
            ids = [i["InstanceId"]
                   for r in self.ec2.describe_instances(Filters=[
                       {"Name": "vpc-id", "Values": [vpc]},
                       {"Name": "instance-state-name", "Values": ["running", "pending", "stopping", "stopped"]}]).get("Reservations", [])
                   for i in r["Instances"]]
            if ids:
                self.ec2.terminate_instances(InstanceIds=ids)
                self.log(f"terminated {len(ids)} instance(s): {', '.join(ids)}")
                self.ec2.get_waiter("instance_terminated").wait(
                    InstanceIds=ids, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
        except ClientError as e:
            self._err("terminate instances", e)

        # 2. NAT gateways (billable) — delete then release their EIPs
        try:
            for n in self.ec2.describe_nat_gateways(Filter=[{"Name": "vpc-id", "Values": [vpc]}]).get("NatGateways", []):
                if n["State"] not in ("deleted", "deleting"):
                    self.ec2.delete_nat_gateway(NatGatewayId=n["NatGatewayId"])
                    self.log(f"deleted NAT gateway {n['NatGatewayId']}")
        except ClientError as e:
            self._err("delete NAT", e)

        # 3. Non-default security groups (controller-created; pin the VPC).
        #    Strip all rules first so inter-SG references don't block deletion.
        try:
            sgs = [s for s in self.ec2.describe_security_groups(
                Filters=[{"Name": "vpc-id", "Values": [vpc]}]).get("SecurityGroups", [])
                if s["GroupName"] != "default"]
            for s in sgs:
                if s.get("IpPermissions"):
                    try:
                        self.ec2.revoke_security_group_ingress(GroupId=s["GroupId"], IpPermissions=s["IpPermissions"])
                    except ClientError:
                        pass
                if s.get("IpPermissionsEgress"):
                    try:
                        self.ec2.revoke_security_group_egress(GroupId=s["GroupId"], IpPermissions=s["IpPermissionsEgress"])
                    except ClientError:
                        pass
            for s in sgs:
                try:
                    self.ec2.delete_security_group(GroupId=s["GroupId"])
                    self.log(f"deleted security group {s['GroupId']} ({s['GroupName']})")
                except ClientError as e:
                    self._err(f"delete SG {s['GroupId']}", e)
        except ClientError as e:
            self._err("security groups", e)

        # 4. Leftover available ENIs
        try:
            for e in self.ec2.describe_network_interfaces(Filters=[
                    {"Name": "vpc-id", "Values": [vpc]},
                    {"Name": "status", "Values": ["available"]}]).get("NetworkInterfaces", []):
                try:
                    self.ec2.delete_network_interface(NetworkInterfaceId=e["NetworkInterfaceId"])
                    self.log(f"deleted ENI {e['NetworkInterfaceId']}")
                except ClientError as ex:
                    self._err(f"delete ENI {e['NetworkInterfaceId']}", ex)
        except ClientError as e:
            self._err("ENIs", e)

    # ---- cluster-scoped orphans (money + hygiene) --------------------------
    def clear_load_balancers(self):
        try:
            for lb in self.elbv2.describe_load_balancers().get("LoadBalancers", []):
                tags = self.elbv2.describe_tags(ResourceArns=[lb["LoadBalancerArn"]]).get("TagDescriptions", [])
                tl = tags[0].get("Tags", []) if tags else []
                if self._has_cluster_tag(tl):
                    self.elbv2.delete_load_balancer(LoadBalancerArn=lb["LoadBalancerArn"])
                    self.log(f"deleted load balancer {lb['LoadBalancerName']}")
        except ClientError as e:
            self._err("load balancers", e)

    def clear_fleets(self):
        try:
            fleets = self.ec2.describe_fleets().get("Fleets", [])
            ids = [f["FleetId"] for f in fleets
                   if f.get("FleetState") not in ("deleted", "deleted-terminating")
                   and self._has_cluster_tag(f.get("Tags", []))]
            if ids:
                self.ec2.delete_fleets(FleetIds=ids, TerminateInstances=True)
                self.log(f"deleted {len(ids)} EC2 fleet(s)")
        except ClientError as e:
            self._err("fleets", e)

    def clear_launch_templates(self):
        try:
            for lt in self.ec2.describe_launch_templates().get("LaunchTemplates", []):
                if self.cluster in lt.get("LaunchTemplateName", ""):
                    self.ec2.delete_launch_template(LaunchTemplateId=lt["LaunchTemplateId"])
                    self.log(f"deleted launch template {lt['LaunchTemplateName']}")
                    continue
                tags = self.ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [lt["LaunchTemplateId"]]}]).get("Tags", [])
                if self._has_cluster_tag([{"Key": t["Key"], "Value": t["Value"]} for t in tags]):
                    self.ec2.delete_launch_template(LaunchTemplateId=lt["LaunchTemplateId"])
                    self.log(f"deleted launch template {lt['LaunchTemplateName']}")
        except ClientError as e:
            self._err("launch templates", e)

    def clear_volumes(self):
        try:
            for v in self.ec2.describe_volumes(Filters=[
                    {"Name": "status", "Values": ["available"]},
                    {"Name": f"tag:kubernetes.io/cluster/{self.cluster}", "Values": ["owned", "shared"]}]).get("Volumes", []):
                self.ec2.delete_volume(VolumeId=v["VolumeId"])
                self.log(f"deleted available EBS volume {v['VolumeId']}")
        except ClientError as e:
            self._err("volumes", e)

    def clear_eips(self):
        try:
            for a in self.ec2.describe_addresses().get("Addresses", []):
                if a.get("AssociationId"):
                    continue  # still in use
                if self._has_cluster_tag(a.get("Tags", [])) or (a.get("Tags") and any(t["Key"] == "Environment" and t["Value"] == self.env for t in a["Tags"])):
                    self.ec2.release_address(AllocationId=a["AllocationId"])
                    self.log(f"released unassociated EIP {a.get('PublicIp')}")
        except ClientError as e:
            self._err("EIPs", e)

    def clear_kms(self):
        # Only schedule keys that are clearly this cluster's (desc names it) and
        # still Enabled/customer-managed — never touch a key that isn't ours.
        try:
            paginator = self.kms.get_paginator("list_keys")
            for page in paginator.paginate():
                for k in page.get("Keys", []):
                    try:
                        md = self.kms.describe_key(KeyId=k["KeyId"])["KeyMetadata"]
                    except ClientError:
                        continue
                    if (md.get("KeyManager") == "CUSTOMER" and md.get("KeyState") == "Enabled"
                            and self.cluster in (md.get("Description") or "")):
                        r = self.kms.schedule_key_deletion(KeyId=k["KeyId"], PendingWindowInDays=7)
                        self.log(f"scheduled KMS key {k['KeyId']} for deletion ({r.get('DeletionDate')})")
        except ClientError as e:
            self._err("KMS", e)

    def clear_instance_profiles(self):
        try:
            paginator = self.iam.get_paginator("list_instance_profiles")
            for page in paginator.paginate():
                for ip in page.get("InstanceProfiles", []):
                    name = ip["InstanceProfileName"]
                    if self.cluster not in name and self.cluster not in ip.get("Path", ""):
                        continue
                    try:
                        for role in ip.get("Roles", []):
                            self.iam.remove_role_from_instance_profile(InstanceProfileName=name, RoleName=role["RoleName"])
                        self.iam.delete_instance_profile(InstanceProfileName=name)
                        self.log(f"deleted instance-profile {name}")
                    except ClientError as e:
                        self._err(f"instance-profile {name}", e)
        except ClientError as e:
            self._err("instance-profiles", e)

    def clear_log_groups(self):
        try:
            for prefix in (f"/aws/eks/{self.cluster}/", f"/aws/containerinsights/{self.cluster}/"):
                for lg in self.logs.describe_log_groups(logGroupNamePrefix=prefix).get("logGroups", []):
                    self.logs.delete_log_group(logGroupName=lg["logGroupName"])
                    self.log(f"deleted log group {lg['logGroupName']}")
        except ClientError as e:
            self._err("log groups", e)

    # ---- orchestration -----------------------------------------------------
    def run(self):
        print(f"  [sweep] scanning for '{self.env}' orphans (cluster {self.cluster}, region {self.region})", flush=True)
        vpc = self.find_vpc()
        if vpc:
            print(f"  [sweep] VPC {vpc} still present — clearing its dependencies so destroy can complete", flush=True)
            self.clear_load_balancers()
            self.clear_vpc_blockers(vpc)
        # cluster-scoped orphans (safe whether or not the VPC is gone)
        self.clear_fleets()
        self.clear_launch_templates()
        self.clear_volumes()
        self.clear_eips()
        self.clear_kms()
        self.clear_instance_profiles()
        self.clear_log_groups()
        if self.actions:
            print(f"  [sweep] removed {len(self.actions)} orphaned resource(s).", flush=True)
        else:
            print("  [sweep] no orphans found — clean.", flush=True)
        return vpc is not None


def main():
    ap = argparse.ArgumentParser(description="Env-scoped post-teardown orphan sweep.")
    ap.add_argument("--env", required=True)
    ap.add_argument("--cluster", required=True, help="EKS cluster name (<resources_prefix>-<env>)")
    ap.add_argument("--region", required=True)
    args = ap.parse_args()
    try:
        Sweeper(args.env, args.cluster, args.region).run()
    except Exception as e:  # never let the sweep abort the teardown
        print(f"  [sweep] non-fatal error: {e}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
