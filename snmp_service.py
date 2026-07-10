import threading
import time
import asyncio
import traceback

from pysnmp.hlapi.asyncio import *
from pysnmp.entity import engine
from pysnmp.entity import config as snmp_config
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.proto import rfc1902

import config
import state

OID_BASE_STR = '1.3.6.1.4.1.99999'
TRAP_OID = f"{OID_BASE_STR}.4.1"

# Kompletna mapa OID -> pole z danych z Arduino.
# .1.1.0        = status połączenia z portem szeregowym
# .2.<n>.0      = pomiary wzmacniacza (zgodne z test_snmp.py: .2.1=p_a_in, .2.5=gain_actual)
STATUS_OID = f"{OID_BASE_STR}.1.1.0"

FIELD_OID_MAP = {
    f"{OID_BASE_STR}.2.1.0": "p_a_in",
    f"{OID_BASE_STR}.2.2.0": "p_a_out",
    f"{OID_BASE_STR}.2.3.0": "p_b_in",
    f"{OID_BASE_STR}.2.4.0": "p_b_out",
    f"{OID_BASE_STR}.2.5.0": "gain_actual",
    f"{OID_BASE_STR}.2.6.0": "gain_set",
    f"{OID_BASE_STR}.2.7.0": "gain_delta",
    f"{OID_BASE_STR}.2.8.0": "temperature",
    f"{OID_BASE_STR}.2.9.0": "seq_nr",
}

# Kolejność OID-ów posortowana numerycznie - potrzebna do obsługi GETNEXT/snmpwalk
ORDERED_OIDS = [STATUS_OID] + sorted(
    FIELD_OID_MAP.keys(),
    key=lambda oid_str: [int(part) for part in oid_str.split(".")]
)

snmp_thread = None
stop_event = threading.Event()


def _read_value_for_oid(oid_str: str):
    """Zwraca wartość (jako string) dla danego OID-a na podstawie aktualnych danych, albo None."""
    with state.state_lock:
        data = getattr(state, "latest_data", {}) or {}
        connected = getattr(state, "serial_connected", False)

    if oid_str == STATUS_OID:
        return "CONNECTED" if connected else "DISCONNECTED"

    field = FIELD_OID_MAP.get(oid_str)
    if field is None:
        return None

    if field not in data:
        return "--"

    return str(data[field])


def _refresh_live_snapshot():
    """Buduje pełny słownik pole -> wartość i zapisuje go do state.latest_snmp_data,
    tak żeby zakładka SNMP na dashboardzie mogła to pokazać bez potrzeby
    odpytywania agenta z zewnątrz (np. snmpwalk)."""
    snapshot = {}

    with state.state_lock:
        data = getattr(state, "latest_data", {}) or {}
        connected = getattr(state, "serial_connected", False)

    snapshot["status"] = "CONNECTED" if connected else "DISCONNECTED"

    for field in FIELD_OID_MAP.values():
        snapshot[field] = data.get(field, "--")

    with state.state_lock:
        state.latest_snmp_data = snapshot


class CustomInstrum:
    def read_variables(self, *args, **kwargs):
        vars_list = args[0] if len(args) > 0 else []
        results = []

        for varBind in vars_list:
            oid, _old_val = varBind
            oid_str = str(oid)

            value = _read_value_for_oid(oid_str)

            if value is None:
                results.append((oid, rfc1902.NoSuchObject("")))
            else:
                results.append((oid, rfc1902.OctetString(value)))

        # Każde zapytanie GET odświeża też snapshot do panelu na stronie
        _refresh_live_snapshot()

        return results

    def read_next_variables(self, *args, **kwargs):
        vars_list = args[0] if len(args) > 0 else []
        results = []

        for varBind in vars_list:
            oid, _old_val = varBind
            oid_str = str(oid)

            next_oid_str = None

            for candidate in ORDERED_OIDS:
                candidate_parts = [int(p) for p in candidate.split(".")]
                oid_parts = [int(p) for p in oid_str.split(".")]

                if candidate_parts > oid_parts:
                    next_oid_str = candidate
                    break

            if next_oid_str is None:
                results.append((oid, rfc1902.EndOfMibView()))
                continue

            value = _read_value_for_oid(next_oid_str)
            next_oid = ObjectIdentifier(next_oid_str) if "ObjectIdentifier" in globals() else oid

            results.append((next_oid, rfc1902.OctetString(value if value is not None else "--")))

        _refresh_live_snapshot()

        return results

    def write_variables(self, *args, **kwargs):
        # Zapis (SET) nieobsługiwany - zwracamy wartości bez zmian
        vars_list = args[0] if len(args) > 0 else []
        return vars_list

    # Alias na wypadek starszej nazwy metody używanej przez niektóre wersje pysnmp
    def writeVars(self, vars_list, acInfo=(None, None)):
        return vars_list


def send_trap(error: dict) -> None:
    asyncio.run(_async_send_trap(error))


async def _async_send_trap(error: dict):
    with state.state_lock:
        snmp_settings = getattr(state, "snmp_settings", {})
        if not snmp_settings.get("enabled", False):
            return
        community = snmp_settings.get("community", "public")
        trap_host = snmp_settings.get("trap_host", "127.0.0.1")
        trap_port = snmp_settings.get("trap_port", 162)

    error_message = (
        f"ALARM: {error.get('field')} | W: {float(error.get('value', 0)):.2f} "
        f"| T: {float(error.get('target', 0)):.2f}"
    )

    try:
        target = await UdpTransportTarget.create((trap_host, trap_port))
        iterator = send_notification(
            SnmpEngine(),
            CommunityData(community, mpModel=1),
            target,
            ContextData(),
            "trap",
            NotificationType(
                ObjectIdentity(TRAP_OID)
            ).addVarBinds(
                ("1.3.6.1.2.1.1.3.0", TimeTicks(int(time.time() * 100))),
                ("1.3.6.1.6.3.1.1.4.1.0", ObjectIdentifier(TRAP_OID)),
                (f"{TRAP_OID}.1", OctetString(error_message)),
            ),
        )
        async for errorIndication, errorStatus, errorIndex, varBinds in iterator:
            if errorIndication:
                print(f"[SNMP TRAP FAIL]: {errorIndication}")
    except Exception as e:
        print(f"[SNMP TRAP ERROR]: {e}")


def _snmp_agent_loop():
    with state.state_lock:
        snmp_settings = getattr(state, "snmp_settings", {})
        port = int(snmp_settings.get("port", 1611))
        community = str(snmp_settings.get("community", "public"))

    print(f"\n[SNMP AGENT] Uruchamianie agenta na 127.0.0.1:{port} z community '{community}'...")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run_agent():
        agent_refs = {}
        try:
            snmpEngine = engine.SnmpEngine()

            transport = udp.UdpTransport().openServerMode(("127.0.0.1", port))
            snmp_config.addTransport(snmpEngine, udp.domainName + (1,), transport)

            snmp_config.addV1System(snmpEngine, "my-area", community)
            snmp_config.addVacmUser(snmpEngine, 2, "my-area", "noAuthNoPriv", (1, 3, 6))

            snmpContext = context.SnmpContext(snmpEngine)
            snmpContext.unregister_context_name("")
            snmpContext.register_context_name("", CustomInstrum())

            responder = cmdrsp.GetCommandResponder(snmpEngine, snmpContext)
            next_responder = cmdrsp.NextCommandResponder(snmpEngine, snmpContext)

            agent_refs["engine"] = snmpEngine
            agent_refs["transport"] = transport
            agent_refs["context"] = snmpContext
            agent_refs["responder"] = responder
            agent_refs["next_responder"] = next_responder

            print("[SNMP AGENT] GOTOWY! Czekam na zapytania...")

            # Niezależnie od tego czy ktoś odpytuje agenta z zewnątrz,
            # co sekundę odświeżamy snapshot dla panelu na dashboardzie.
            while not stop_event.is_set():
                _refresh_live_snapshot()
                await asyncio.sleep(1)

        except Exception as e:
            print(f"\n[SNMP AGENT KRYTYCZNY BŁĄD]: {e}")
            traceback.print_exc()

    loop.run_until_complete(run_agent())
    loop.close()
    print("[SNMP AGENT] Zatrzymano agenta.")


def init_snmp():
    with state.state_lock:
        snmp_settings = getattr(state, "snmp_settings", {})
        if not snmp_settings.get("enabled", False):
            print("[SNMP] Usługa wyłączona w ustawieniach (enabled=False).")
            return

    global snmp_thread
    stop_event.clear()
    snmp_thread = threading.Thread(target=_snmp_agent_loop, daemon=True)
    snmp_thread.start()


def close_snmp():
    stop_event.set()
    if snmp_thread:
        snmp_thread.join(timeout=2)