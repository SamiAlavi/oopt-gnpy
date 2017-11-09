#!/usr/bin/env python3

from collections import namedtuple

class ConvenienceAccess:
    def __init_subclass__(cls):
        for abbrev, field in getattr(cls, '_ABBREVS', {}).items():
            setattr(cls, abbrev, property(lambda self, f=field: getattr(self, f)))

    def update(self, **kwargs):
        for abbrev, field in getattr(self, '_ABBREVS', {}).items():
            if abbrev in kwargs:
                kwargs[field] = kwargs.pop(abbrev)
        return self._replace(**kwargs)

class Power(namedtuple('Power', 'signal nonlinear_interference amplified_spontaneous_emission'), ConvenienceAccess):
    _ABBREVS = {'nli': 'nonlinear_interference',
                'ase': 'amplified_spontaneous_emission',}

class Carrier(namedtuple('Carrier', 'channel_number frequency modulation baud_rate alpha power'), ConvenienceAccess):
    _ABBREVS = {'channel': 'channel_number',
                'ch':      'channel_number',
                'ffs':     'frequency',
                'freq':    'frequency',}

class SpectralInformation(namedtuple('SpectralInformation', 'carriers'), ConvenienceAccess):
    def __new__(cls, *carriers):
        return super().__new__(cls, carriers)

if __name__ == '__main__':
    si = SpectralInformation(
        Carrier(1, 193.95e12, '16-qam', 32e9, 0,  # 193.95 THz, 32 Gbaud
            Power(1e-3, 1e-6, 1e-6)),             # 1 mW, 1uW, 1uW
        Carrier(1, 195.95e12, '16-qam', 32e9, 0,  # 195.95 THz, 32 Gbaud
            Power(1.2e-3, 1e-6, 1e-6)),           # 1.2 mW, 1uW, 1uW
    )
    print(f'si = {si}')
    print(f'si = {si.carriers[0].power.nli}')
    si2 = si.update(carriers=tuple(c.update(power = c.power.update(nli = c.power.nli * 1e5))
                              for c in si.carriers))
    print(f'si2 = {si2}')
