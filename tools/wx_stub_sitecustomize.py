"""Stub out wxPython so Pyfa's eos engine imports headless (see pyfa_reference.py)."""
import sys, types, importlib.abc, importlib.machinery
from unittest.mock import MagicMock
class _M(types.ModuleType):
    def __getattr__(self, n):
        if n.startswith('__'): raise AttributeError(n)
        m = MagicMock(name=self.__name__+'.'+n); setattr(self, n, m); return m
class _F(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):
        if name == 'wx' or name.startswith('wx.'):
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
    def create_module(self, spec):
        m = _M(spec.name); m.__path__ = []
        if spec.name == 'wx.lib.newevent':
            m.NewEvent = lambda: (type('E',(object,),{}), 1); m.NewCommandEvent = m.NewEvent
        return m
    def exec_module(self, m): pass
sys.meta_path.insert(0, _F())
