#!/usr/bin/env python3
"""Tenta criar uma VM Always Free na OCI (Oracle Cloud) ate conseguir."""
import os
import sys
import time

import oci

VM_NAME = os.environ.get("VM_NAME", "dtpromo")
BUDGET_SECONDS = int(os.environ.get("BUDGET_SECONDS", "200"))
SHAPE = "VM.Standard.A1.Flex"
SHAPE_CONFIGS = [(2, 12), (1, 6)]  # (ocpus, memory_in_gbs)
SLEEP_BETWEEN_ATTEMPTS = 15
CLIENT_TIMEOUT = (10, 60)


def log(msg):
    print(msg, flush=True)


def mask(value):
    if value:
        print(f"::add-mask::{value}", flush=True)


def write_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        f.write(f"{key}={value}\n")


def build_config():
    config = {
        "user": os.environ["OCI_USER"],
        "tenancy": os.environ["OCI_TENANCY"],
        "fingerprint": os.environ["OCI_FINGERPRINT"],
        "region": os.environ["OCI_REGION"],
        "key_content": os.environ["OCI_KEY"],
    }
    oci.config.validate_config(config)
    return config


def make_clients(config):
    retry_strategy = oci.retry.NoneRetryStrategy()
    identity = oci.identity.IdentityClient(
        config, timeout=CLIENT_TIMEOUT, retry_strategy=retry_strategy
    )
    compute = oci.core.ComputeClient(
        config, timeout=CLIENT_TIMEOUT, retry_strategy=retry_strategy
    )
    network = oci.core.VirtualNetworkClient(
        config, timeout=CLIENT_TIMEOUT, retry_strategy=retry_strategy
    )
    return identity, compute, network


def vm_already_exists(compute, compartment_id):
    instances = oci.pagination.list_call_get_all_results(
        compute.list_instances, compartment_id
    ).data
    for instance in instances:
        mask(instance.id)
        if (
            instance.display_name == VM_NAME
            and instance.lifecycle_state not in ("TERMINATED", "TERMINATING")
        ):
            return True
    return False


def discover_resources(identity, compute, network, compartment_id):
    try:
        ads = identity.list_availability_domains(compartment_id=compartment_id).data
        for ad in ads:
            mask(ad.id)

        subnets = oci.pagination.list_call_get_all_results(
            network.list_subnets, compartment_id
        ).data
        subnet = None
        for s in subnets:
            mask(s.id)
            if "public" in (s.display_name or "").lower():
                subnet = s
                break

        images = compute.list_images(
            compartment_id=compartment_id,
            operating_system="Canonical Ubuntu",
            operating_system_version="24.04",
            shape=SHAPE,
            sort_by="TIMECREATED",
            sort_order="DESC",
        ).data
        image = images[0] if images else None
        if image:
            mask(image.id)
    except oci.exceptions.ServiceError as e:
        log(f"erro ao consultar recursos: status={e.status} code={e.code}")
        sys.exit(1)

    if subnet is None or image is None:
        log("recursos obrigatorios nao encontrados (subnet publica ou imagem)")
        sys.exit(1)

    log("auth ok")
    log(f"dominios de disponibilidade encontrados: {len(ads)}")
    log(f"imagem selecionada: {image.display_name}")
    return ads, subnet, image


def try_launch(compute, compartment_id, ads, subnet, image, ssh_pubkey):
    for ocpus, mem in SHAPE_CONFIGS:
        for ad in ads:
            details = oci.core.models.LaunchInstanceDetails(
                compartment_id=compartment_id,
                availability_domain=ad.name,
                shape=SHAPE,
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=ocpus, memory_in_gbs=mem
                ),
                display_name=VM_NAME,
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=subnet.id, assign_public_ip=True
                ),
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    image_id=image.id
                ),
                metadata={"ssh_authorized_keys": ssh_pubkey},
            )
            try:
                response = compute.launch_instance(details)
                return response.data, (ocpus, mem)
            except oci.exceptions.ServiceError as e:
                msg = (e.message or "").lower()
                if "capacity" in msg:
                    log("sem capacidade, tentando novamente")
                elif e.status == 429:
                    log(f"limite de taxa atingido (status={e.status})")
                    sys.exit(0)
                elif e.status in (400, 401, 403, 404):
                    log(f"erro de requisicao status={e.status} code={e.code}")
                    sys.exit(1)
                else:
                    log(f"erro de servico status={e.status} code={e.code}")
            except Exception as e:
                log(f"erro inesperado: {type(e).__name__}")
            time.sleep(SLEEP_BETWEEN_ATTEMPTS)
    return None, None


def main():
    mask(os.environ.get("OCI_USER"))
    mask(os.environ.get("OCI_TENANCY"))
    mask(os.environ.get("OCI_FINGERPRINT"))

    config = build_config()
    identity, compute, network = make_clients(config)
    compartment_id = config["tenancy"]
    ssh_pubkey = os.environ.get("SSH_PUBKEY", "").strip()

    if vm_already_exists(compute, compartment_id):
        log("VM ja existe")
        write_output("disable", "true")
        sys.exit(0)

    ads, subnet, image = discover_resources(identity, compute, network, compartment_id)

    start = time.time()
    first_cycle = True
    instance = None
    shape_used = None

    while first_cycle or (time.time() - start) < BUDGET_SECONDS:
        first_cycle = False
        instance, shape_used = try_launch(
            compute, compartment_id, ads, subnet, image, ssh_pubkey
        )
        if instance is not None:
            break
        if (time.time() - start) >= BUDGET_SECONDS:
            break

    if instance is None:
        log("nao foi possivel criar a VM neste ciclo")
        sys.exit(0)

    mask(instance.id)
    ocpus, mem = shape_used
    log(f"VM CRIADA ({ocpus} OCPU / {mem} GB)")

    try:
        oci.wait_until(
            compute,
            compute.get_instance(instance.id),
            "lifecycle_state",
            "RUNNING",
            max_wait_seconds=600,
        )
    except Exception:
        log("nao foi possivel confirmar o estado final dentro do tempo do job")

    write_output("disable", "true")
    sys.exit(0)


if __name__ == "__main__":
    main()
