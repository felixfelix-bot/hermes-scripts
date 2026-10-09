import importlib.util, json, os, sys
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location('dispatch_gate', os.path.join(ROOT, 'dispatch_gate.py'))
dg = importlib.util.module_from_spec(spec); spec.loader.exec_module(dg)

POLICY = {'boards': ['fips', 'contextvm-services'], 'max_load_per_cpu': 0.8,
          'min_mem_available_mb': 1536, 'max_swap_used_pct': 90}

def m(load=0.1, cores=4, mem=8000, swap_total=1000, swap_free=1000):
    return {'load1': load, 'cores': cores, 'mem_available_mb': mem,
            'swap_total_mb': swap_total, 'swap_free_mb': swap_free}

def test_allow_when_idle():
    assert dg.decide(m(), POLICY)['allow'] is True

def test_deny_on_load_per_cpu_not_absolute():
    # 11.0 on 4 cores is 2.75/CPU -> deny. The old gate used an ABSOLUTE 3.4,
    # which is arbitrary on any core count.
    assert dg.decide(m(load=11.0, cores=4), POLICY)['allow'] is False

def test_same_absolute_load_allowed_on_many_cores():
    # Per-CPU policy must scale with the machine: 11.0 on 16 cores = 0.69/CPU.
    assert dg.decide(m(load=11.0, cores=16), POLICY)['allow'] is True

def test_deny_on_low_memory():
    assert dg.decide(m(mem=1200), POLICY)['allow'] is False

def test_deny_on_swap_pressure():
    r = dg.decide(m(swap_total=1000, swap_free=50), POLICY)
    assert r['allow'] is False and 'swap' in r['reason']

def test_reason_names_the_binding_dimension():
    r = dg.decide(m(load=11.0, cores=4), POLICY)
    assert r['allow'] is False and 'load' in r['reason']

def test_policy_file_lists_the_contextvm_board():
    p = json.load(open(os.path.join(ROOT, 'config', 'dispatch_policy.json')))
    assert 'contextvm-services' in p['boards']
    assert p['max_load_per_cpu'] > 0 and p['min_mem_available_mb'] > 0
