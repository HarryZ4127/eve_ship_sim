"""Compute reference stats for EFT fits with Pyfa's eos engine (All-V character).

Used to cross-check evesim against Pyfa. Pyfa's GUI is not needed: wx is stubbed.

    git clone --depth 1 https://github.com/pyfa-org/Pyfa /path/to/pyfa
    python -m venv venv && venv/bin/pip install sqlalchemy logbook numpy python-dateutil requests \
        pyyaml roman packaging beautifulsoup4 markdown2 cryptography python-jose requests-cache
    mkdir stubs && cp tools/wx_stub_sitecustomize.py stubs/sitecustomize.py
    (cd /path/to/pyfa && PYTHONPATH=$PWD/../stubs ../venv/bin/python db_update.py)
    PYTHONPATH=stubs venv/bin/python tools/pyfa_reference.py /path/to/pyfa fits.json > out.json

``fits.json`` maps names to EFT text. Drones are activated in listed order up to
5 and the hull's bandwidth, the same rule evesim uses.
"""
import sys, os, json
PYFA = sys.argv[1]
sys.path.insert(0, PYFA)
os.chdir(PYFA)
import config
config.debug = False
config.saveInRoot = True
config.defPaths(None)
config.saveDB = ':memory:'
import eos.config
eos.config.saveddata_connectionstring = 'sqlite:///:memory:'
import eos.db
eos.db.saveddata_meta.create_all(eos.db.saveddata_engine)
from eos.saveddata.fit import Fit
from eos.saveddata.ship import Ship
from eos.saveddata.module import Module
from eos.saveddata.drone import Drone
from eos.const import FittingSlot
from eos.saveddata.character import Character
from eos.const import FittingModuleState
from eos.utils.spoolSupport import SpoolType, SpoolOptions

def stats(fit):
    fit.character = Character.getAll5()
    fit.calculateModifiedAttributes()
    s = fit.ship
    dps = fit.getTotalDps().total
    weap = fit.getWeaponDps().total
    drone = fit.getDroneDps().total
    volley = fit.getTotalVolley().total
    ehp = fit.ehp
    res = {}
    for layer, pre in (('shield','shield'),('armor','armor'),('hull','')):
        for dt in ('Em','Thermal','Kinetic','Explosive'):
            a = (pre + dt + 'DamageResonance') if pre else (dt[0].lower()+dt[1:]+'DamageResonance')
            res[f'{layer}_{dt.lower()}'] = s.getModifiedItemAttr(a)
    cap = {'stable': fit.capStable, 'state': fit.capState, 'used_per_s': fit.capUsed, 'recharge_peak': fit.capRecharge}
    out = {
        'dps': dps, 'weapon_dps': weap, 'drone_dps': drone, 'volley': volley,
        'ehp': ehp, 'hp': fit.hp,
        'resonance': res,
        'cpu_used': fit.cpuUsed, 'cpu_out': s.getModifiedItemAttr('cpuOutput'),
        'pg_used': fit.pgUsed, 'pg_out': s.getModifiedItemAttr('powerOutput'),
        'calibration_used': fit.calibrationUsed,
        'max_velocity': fit.maxSpeed, 'align_time': fit.alignTime,
        'sig_radius': s.getModifiedItemAttr('signatureRadius'),
        'mass': s.getModifiedItemAttr('mass'),
        'lock_range': s.getModifiedItemAttr('maxTargetRange'),
        'scan_res': s.getModifiedItemAttr('scanResolution'),
        'cap_capacity': s.getModifiedItemAttr('capacitorCapacity'),
        'cap_recharge_ms': s.getModifiedItemAttr('rechargeRate'),
        'shield_recharge_ms': s.getModifiedItemAttr('shieldRechargeRate'),
        'cap': cap,
        'tank_sustained': fit.sustainableTank, 'tank_peak': fit.tank,
        'drone_bw': s.getModifiedItemAttr('droneBandwidth'),
        'modules': [],
    }
    for m in fit.modules:
        if m.item is None: continue
        d = {'name': m.item.name, 'state': int(m.state)}
        for a in ('damageMultiplier','speed','maxRange','falloff','trackingSpeed','cpu','power','capacitorNeed','duration','shieldBonus','armorDamageAmount'):
            v = m.getModifiedItemAttr(a)
            if v: d[a] = v
        if m.charge:
            d['charge'] = m.charge.name
            for a in ('emDamage','thermalDamage','kineticDamage','explosiveDamage','maxVelocity','explosionDelay','aoeCloudSize','aoeVelocity'):
                v = m.getModifiedChargeAttr(a)
                if v: d['charge_'+a] = v
        out['modules'].append(d)
    out['drones'] = [{'name': d.item.name, 'amount': d.amount, 'active': d.amountActive, 'damageMultiplier': d.getModifiedItemAttr('damageMultiplier'), 'speed': d.getModifiedItemAttr('speed'), 'thermalDamage': d.getModifiedItemAttr('thermalDamage'), 'kineticDamage': d.getModifiedItemAttr('kineticDamage'),'emDamage': d.getModifiedItemAttr('emDamage'),'explosiveDamage': d.getModifiedItemAttr('explosiveDamage')} for d in fit.drones]
    return out

def parse_eft(text):
    lines = [l.strip() for l in text.strip().splitlines()]
    ship_name = lines[0][1:-1].split(',')[0].strip()
    fit = Fit(Ship(eos.db.getItem(ship_name)))
    fit._left = [5, eos.db.getItem(ship_name).getAttribute('droneBandwidth') or 0]
    for l in lines[1:]:
        if not l or l.startswith('['): continue
        state = None
        if l.endswith('/OFFLINE'): l = l[:-8].strip(); state = FittingModuleState.OFFLINE
        if l.endswith('/OVERHEAT'): l = l[:-9].strip(); state = FittingModuleState.OVERHEATED
        parts = l.rsplit(' x', 1)
        if len(parts) == 2 and parts[1].isdigit():
            item = eos.db.getItem(parts[0].strip())
            if item.category.name == 'Drone':
                d = Drone(item); d.amount = int(parts[1]); n = min(d.amount, fit._left[0], int(fit._left[1] // item.getAttribute('droneBandwidthUsed'))); d.amountActive = n; fit._left[0] -= n; fit._left[1] -= n * item.getAttribute('droneBandwidthUsed'); d.owner = fit; fit.drones.append(d); continue
            continue  # cargo
        mname, _, cname = l.partition(',')
        it = eos.db.getItem(mname.strip())
        if it is None: raise SystemExit("unknown item " + mname)
        m = Module(it)
        if cname.strip(): m.charge = eos.db.getItem(cname.strip())
        m.owner = fit; fit.modules.append(m)
        if state is not None: m.state = state
        elif m.isValidState(FittingModuleState.ACTIVE): m.state = FittingModuleState.ACTIVE
        else: m.state = FittingModuleState.ONLINE
    return fit

fits = json.load(open(sys.argv[2]))
result = {}
for name, eft in fits.items():
    fit = parse_eft(eft)
    result[name] = stats(fit)
json.dump(result, sys.stdout, indent=1, default=str)
