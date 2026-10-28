#!/usr/bin/env python3
"""Tenta criar uma VM Always Free na OCI (Oracle Cloud) ate conseguir.

Roda em loop continuo dentro de um unico job (ate BUDGET_SECONDS), checando
a existencia da VM antes de cada tentativa de lancamento para nunca criar
duas instancias, mesmo sob erros transitorios de rede.
"""
import os
import sys
import time

import oci

VM_NAME = os.environ.get("VM_NAME", "dtpromo")
BUDGET_SECONDS = int(os.environ.get("BUDGET_SECONDS", "20400"))
CYCLE_SLEEP = int(os.environ.get("CYCLE_SLEEP", "60"))
SUMMARY_EVERY = int(os.environ.get("SUMMARY_EVERY", "600"))
BACKOFF_MAX = int(os.environ.get("BACKOFF_MAX", "600"))
SHAPE = "VM.Standard.A1.Flex"
SHAPE_CONFIGS = [(2, 12), (1, 6)]  # (ocpus, memory_in_gbs)
CLIENT_TIMEOUT = (10, 60)
PAUSE_BETWEEN_CALLS = 3


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


def fmt_elapsed(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def next_backoff(level):
    return min(120 * (2 ** level), BACKOFF_MAX)


def sleep_respecting_budget(seconds, start):
    remaining = BUDGET_SECONDS - (time.time() - start)
    if remaining <= 0:
        return False
    time.sleep(min(seconds, remaining))
    return True


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


def classify_service_error(e):
    message = (e.message or "").lower()
    if "capacity" in message:
        return "capacity"
    if e.status == 429:
        return "rate_limit"
    if e.status in (400, 401, 403, 404):
        return "config"
    code = e.code or ""
    if code in ("NotAuthenticated", "NotAuthorized"):
        return "config"
    return "transient"


def list_and_check_exists(compute, compartment_id):
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

    if subnet is None or image is None:
        log("recursos obrigatorios nao encontrados (subnet publica ou imagem)")
        sys.exit(1)

    log("auth ok")
    log(f"dominios de disponibilidade encontrados: {len(ads)}")
    log(f"imagem selecionada: {image.display_name}")
    return ads, subnet, image


def discover_resources_with_retry(identity, compute, network, compartment_id, start):
    backoff_level = 0
    while True:
        remaining = BUDGET_SECONDS - (time.time() - start)
        if remaining <= 0:
            log("orcamento esgotado durante a descoberta de recursos")
            write_output("chain", "true")
            sys.exit(0)
        try:
            return discover_resources(identity, compute, network, compartment_id)
        except oci.exceptions.ServiceError as e:
            kind = classify_service_error(e)
            if kind == "config":
                log(f"erro de descoberta status={e.status} code={e.code}")
                sys.exit(1)
            wait = min(next_backoff(backoff_level), max(0, int(remaining)))
            backoff_level += 1
            log(f"descoberta: erro status={e.status} code={e.code}, nova tentativa em {wait}s")
            time.sleep(wait)
        except SystemExit:
            raise
        except Exception as e:
            wait = min(next_backoff(backoff_level), max(0, int(remaining)))
            backoff_level += 1
            log(f"descoberta: erro inesperado {type(e).__name__}, nova tentativa em {wait}s")
            time.sleep(wait)


def run_cycle(compute, compartment_id, ads, subnet, image, ssh_pubkey, stats):
    """Executa um ciclo completo de tentativas. Verifica a existencia da VM
    antes de CADA chamada launch_instance, para nunca criar duas instancias."""
    for ocpus, mem in SHAPE_CONFIGS:
        for ad in ads:
            try:
                exists = list_and_check_exists(compute, compartment_id)
            except oci.exceptions.ServiceError as e:
                kind = classify_service_error(e)
                if kind == "config":
                    log(f"erro de autenticacao/configuracao status={e.status} code={e.code}")
                    sys.exit(1)
                if kind == "rate_limit":
                    stats["rate_limit"] += 1
                    return {"status": "rate_limit"}
                stats["other"] += 1
                return {"status": "transient", "detail": f"ServiceError status={e.status} code={e.code}"}
            except Exception as e:
                stats["other"] += 1
                return {"status": "transient", "detail": type(e).__name__}

            if exists:
                log("VM ja existe")
                write_output("disable", "true")
                sys.exit(0)

            time.sleep(PAUSE_BETWEEN_CALLS)

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
            stats["attempts"] += 1
            try:
                response = compute.launch_instance(details)
                return {"status": "launched", "instance": response.data, "shape": (ocpus, mem)}
            except oci.exceptions.ServiceError as e:
                kind = classify_service_error(e)
                if kind == "capacity":
                    stats["no_capacity"] += 1
                    time.sleep(PAUSE_BETWEEN_CALLS)
                    continue
                if kind == "config":
                    log(f"erro de requisicao status={e.status} code={e.code}")
                    sys.exit(1)
                if kind == "rate_limit":
                    stats["rate_limit"] += 1
                    return {"status": "rate_limit"}
                stats["other"] += 1
                return {"status": "transient", "detail": f"ServiceError status={e.status} code={e.code}"}
            except Exception as e:
                stats["other"] += 1
                return {"status": "transient", "detail": type(e).__name__}

    return {"status": "no_capacity"}


def summary_line(cycle, stats, elapsed):
    return (
        f"== resumo: {cycle} ciclos, {stats['attempts']} tentativas, "
        f"sem capacidade={stats['no_capacity']}, 429={stats['rate_limit']}, "
        f"outros erros={stats['other']}, tempo decorrido {fmt_elapsed(elapsed)} =="
    )


def main():
    mask(os.environ.get("OCI_USER"))
    mask(os.environ.get("OCI_TENANCY"))
    mask(os.environ.get("OCI_FINGERPRINT"))

    config = build_config()
    identity, compute, network = make_clients(config)
    compartment_id = config["tenancy"]
    ssh_pubkey = os.environ.get("SSH_PUBKEY", "").strip()

    start = time.time()
    ads, subnet, image = discover_resources_with_retry(
        identity, compute, network, compartment_id, start
    )

    stats = {"attempts": 0, "no_capacity": 0, "rate_limit": 0, "other": 0}
    cycle = 0
    backoff_level = 0
    last_summary = start

    while (time.time() - start) < BUDGET_SECONDS:
        cycle += 1
        result = run_cycle(compute, compartment_id, ads, subnet, image, ssh_pubkey, stats)

        if result["status"] == "launched":
            instance = result["instance"]
            ocpus, mem = result["shape"]
            mask(instance.id)
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

        elapsed_str = fmt_elapsed(time.time() - start)

        if result["status"] == "rate_limit":
            wait = next_backoff(backoff_level)
            backoff_level += 1
            log(f"[ciclo {cycle} | {elapsed_str}] 429 -> espera {wait}s")
            if not sleep_respecting_budget(wait, start):
                break
        else:
            backoff_level = 0
            if result["status"] == "no_capacity":
                shapes = ", ".join(f"{o}/{m}" for o, m in SHAPE_CONFIGS)
                log(f"[ciclo {cycle} | {elapsed_str}] sem capacidade ({shapes}) | proximo em {CYCLE_SLEEP}s")
            else:
                log(f"[ciclo {cycle} | {elapsed_str}] erro transitorio {result['detail']} | proximo em {CYCLE_SLEEP}s")
            if not sleep_respecting_budget(CYCLE_SLEEP, start):
                break

        now = time.time()
        if now - last_summary >= SUMMARY_EVERY:
            log(summary_line(cycle, stats, now - start))
            last_summary = now

    log(summary_line(cycle, stats, time.time() - start))
    write_output("chain", "true")
    sys.exit(0)


if __name__ == "__main__":
    main()
