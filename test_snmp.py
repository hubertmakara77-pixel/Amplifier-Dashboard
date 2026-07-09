import asyncio
import json
from pysnmp.hlapi.asyncio import *

async def test_agent():
    print("Wczytuję ustawienia bezpośrednio z pliku persisted_state.json...")
    try:
        with open('persisted_state.json', 'r') as f:
            state_data = json.load(f)
            snmp_settings = state_data.get('snmp_settings', {})
            port = snmp_settings.get('port', 1611)
            community = snmp_settings.get('community', 'public')
            enabled = snmp_settings.get('enabled', False)
    except Exception as e:
        print("Nie znaleziono pliku, używam domyślnych: 1611 / public")
        port = 1611
        community = 'public'
        enabled = False
        
    print(f"-> Konfiguracja docelowa: Port={port}, Community='{community}', Włączony={enabled}")
    
    if not enabled:
        print("\n[OSTRZEŻENIE]: Plik JSON twierdzi, że Agent jest WYŁĄCZONY!")
        print("Musisz kliknąć 'Save' na stronie internetowej, aby go włączyć przed testem.")
        
    print("\nWysyłam zapytanie GET do Agenta SNMP...")
    target = await UdpTransportTarget.create(('127.0.0.1', port)) 
    
    errorIndication, errorStatus, errorIndex, varBinds = await get_cmd(
        SnmpEngine(),
        CommunityData(community),
        target,
        ContextData(),
        ObjectType(ObjectIdentity('1.3.6.1.4.1.99999.1.1.0')), # Status połączenia
        ObjectType(ObjectIdentity('1.3.6.1.4.1.99999.2.1.0')), # Port A IN
        ObjectType(ObjectIdentity('1.3.6.1.4.1.99999.2.5.0')), # Gain Actual
    )

    if errorIndication:
        print(f"\n[BŁĄD KOMUNIKACJI]: {errorIndication}")
        print("Brak odpowiedzi. Oznacza to, że serwer główny na pewno nie nasłuchuje na tym porcie.")
    elif errorStatus:
        print(f"\n[BŁĄD SNMP]: {errorStatus.prettyPrint()}")
    else:
        print("\n[SUKCES] Odczytano parametry z pracującego wzmacniacza:")
        for varBind in varBinds:
            print(' = '.join([x.prettyPrint() for x in varBind]))

if __name__ == '__main__':
    asyncio.run(test_agent())