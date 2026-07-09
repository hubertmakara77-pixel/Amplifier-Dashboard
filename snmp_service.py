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

snmp_thread = None
stop_event = threading.Event()

class CustomInstrum:
    # Wersja 6.x wysyła do tej metody 3 argumenty: self, varBinds, i kontekst (jako kwargs)
    # Dodanie *args i **kwargs czyni funkcję odporną na liczbę argumentów
    def read_variables(self, *args, **kwargs):
        vars_list = args[0] if len(args) > 0 else []
        results = []
        
        # Tworzymy słownik, który będzie widoczny na stronie
        snmp_snapshot = {}

        for varBind in vars_list:
            oid, val = varBind
            oid_str = str(oid)
            
            with state.state_lock:
                data = getattr(state, 'latest_data', {})
                # ... (twoja logika mapowania OID) ...
                # np. dla p_a_in:
                if oid_str == f"{OID_BASE_STR}.2.1.0":
                    val_str = str(data.get('p_a_in', '--'))
                    results.append((oid, rfc1902.OctetString(val_str)))
                    snmp_snapshot['p_a_in'] = val_str
            
        # Zapisujemy to do stanu, aby API mogło to pobrać
        with state.state_lock:
            state.latest_snmp_data = snmp_snapshot
            
        return results
    
    def read_next_variables(self, *args, **kwargs):
        return []
    def writeVars(self, vars, acInfo=(None, None)):
        return vars
        
    def read_next_variables(self, *args, **kwargs):
        return [(oid, rfc1902.EndOfMibView()) for oid, val in vars]

def send_trap(error: dict) -> None:
    asyncio.run(_async_send_trap(error))

async def _async_send_trap(error: dict):
    with state.state_lock:
        snmp_settings = getattr(state, 'snmp_settings', {})
        if not snmp_settings.get("enabled", False):
            return
        community = snmp_settings.get("community", "public")
        trap_host = snmp_settings.get("trap_host", "127.0.0.1")
        trap_port = snmp_settings.get("trap_port", 162)

    error_message = f"ALARM: {error.get('field')} | W: {float(error.get('value', 0)):.2f} | T: {float(error.get('target', 0)):.2f}"

    try:
        target = await UdpTransportTarget.create((trap_host, trap_port))
        iterator = send_notification(
            SnmpEngine(),
            CommunityData(community, mpModel=1),
            target,
            ContextData(),
            'trap',
            NotificationType(
                ObjectIdentity(TRAP_OID)
            ).addVarBinds(
                ('1.3.6.1.2.1.1.3.0', TimeTicks(int(time.time() * 100))),
                ('1.3.6.1.6.3.1.1.4.1.0', ObjectIdentifier(TRAP_OID)),
                (f"{TRAP_OID}.1", OctetString(error_message))
            )
        )
        async for errorIndication, errorStatus, errorIndex, varBinds in iterator:
            if errorIndication:
                print(f"[SNMP TRAP FAIL]: {errorIndication}")
    except Exception as e:
        print(f"[SNMP TRAP ERROR]: {e}")

def _snmp_agent_loop():
    with state.state_lock:
        snmp_settings = getattr(state, 'snmp_settings', {})
        port = int(snmp_settings.get("port", 1611))
        community = str(snmp_settings.get("community", "public"))
    
    print(f"\n[SNMP AGENT] Uruchamianie agenta na 127.0.0.1:{port} z hasłem '{community}'...")
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def run_agent():
        # Słownik ratujący obiekty przed usunięciem przez Garbage Collector!
        agent_refs = {}
        try:
            snmpEngine = engine.SnmpEngine()
            
            # Nasłuchujemy stricte na 127.0.0.1, by pominąć blokady Windowsa
            transport = udp.UdpTransport().openServerMode(('127.0.0.1', port))
            snmp_config.addTransport(snmpEngine, udp.domainName + (1,), transport)
            
            snmp_config.addV1System(snmpEngine, 'my-area', community)
            snmp_config.addVacmUser(snmpEngine, 2, 'my-area', 'noAuthNoPriv', (1, 3, 6))
            
            snmpContext = context.SnmpContext(snmpEngine)
            # Poprawiona nazwa metody: unregister_context_name zamiast unregisterContextName
            snmpContext.unregister_context_name('')
            # Poprawiona nazwa metody: register_context_name zamiast registerContextName
            snmpContext.register_context_name('', CustomInstrum())

            responder = cmdrsp.GetCommandResponder(snmpEngine, snmpContext)

            # Silne referencje
            agent_refs['engine'] = snmpEngine
            agent_refs['transport'] = transport
            agent_refs['context'] = snmpContext
            agent_refs['responder'] = responder

            print(f"[SNMP AGENT] GOTOWY! Czekam na zapytania...")

            while not stop_event.is_set():
                await asyncio.sleep(1)
                
        except Exception as e:
            print(f"\n[SNMP AGENT KRYTYCZNY BŁĄD]: {e}")
            traceback.print_exc()

    loop.run_until_complete(run_agent())
    loop.close()
    print("[SNMP AGENT] Zatrzymano agenta.")

def init_snmp():
    with state.state_lock:
        snmp_settings = getattr(state, 'snmp_settings', {})
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