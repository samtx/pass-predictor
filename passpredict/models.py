# models.py
from dataclasses import dataclass
from functools import cached_property
import math

import numpy as np
from astropy.time import Time

from .schemas import Location, Point, Overpass, PassType
from .constants import RAD2DEG
from .rotations import ecef2sez
from .topocentric import site_ECEF
from .timefn import jday2datetime


class SpaceObject:
    __slots__ = ['time', 'rECEF', 'rECI']
    def __init__(self):
        self.time = None  # time object
        self.rECEF = None
        self.rECI = None
        
        
class Sun(SpaceObject):
    pass


class Sat(SpaceObject):
    def __init__(self):
        self.latitude = None
        self.longitude = None
        self.altitude = None
        self.illuminated = None
        self.rECEF_astropy = None
        self.subpoint = None
        self.id = None


@dataclass
class SunPredictData:
    __slots__ = ['rECEF']
    rECEF: np.ndarray


@dataclass
class SatPredictData:
    __slots__ = ['id', 'rECEF', 'illuminated']
    id: int
    rECEF: np.ndarray         # dtype np.float32
    illuminated: np.ndarray   # dtype bool


class RhoVectorBase:
    pass


class RhoVector():
    """
    Vector from topographic location to space object
    """
    # __slots__ = ['time', 'rSEZ', 'rECEF', 'rng', 'az', 'el', 'ra', 'dec', 'sat', 'location']
    def __init__(self, t: Time, sat: SpaceObject, location: Location, sun: Sun = None):
        self.sat = sat
        self.location = location
        self.time = t
        
        if sun is not None:
            # If the sun variable is set, it's time object must be identical to the sat
            # assert np.all(sun.time.jd[[0, -1]], sat.time.jd[[0, -1]])
            assert sat.illuminated is not None
            self.sun = sun
            self.site_sun_rho = RhoVector(t, sun, location)
        else:
            self.sun = sun
            self.site_sun_rho = None

    @cached_property
    def rsiteECEF(self):
        r = site_ECEF(self.location.lat, self.location.lon, self.location.h)
        return np.array([[r[0]],[r[1]],[r[2]]], dtype=np.float64)
    
    def  _rECEF(self):
        return self.sat.rECEF - self.rsiteECEF

    @cached_property
    def rECEF(self):
        return self._rECEF()

    def _rSEZ(self):
        return ecef2sez(self.rECEF, self.location.lat, self.location.lon) 

    @cached_property
    def rSEZ(self):
        return self._rSEZ()

    def _rng(self):
        return np.linalg.norm(self.rSEZ, axis=0)

    @cached_property
    def rng(self):
        return self._rng()

    def _el(self):
        return np.arcsin(self.rSEZ[2] / self.rng) * RAD2DEG

    @cached_property
    def el(self):
        return self._el()

    def az(self, idx):
        rS = self.rSEZ[0, idx]
        rE = self.rSEZ[1, idx]
        tmp = np.arctan2(rS, rE)
        az = (tmp + np.pi * 0.5) * RAD2DEG
        if rS < 0 and rE < 0:
            az %= 360 
        # idx = np.all([self.rSEZ[0] < 0, self.rSEZ[1] < 0], axis=0)
        # az[idx] %= 360 
        return az

    def point(self, idx):
        return Point.construct(
            datetime=jday2datetime(self.time.jd[idx]),
            azimuth=round(self.az(idx), 3),
            elevation=round(self.el[idx], 3),
            range=round(self.rng[idx], 4)
        )

    def _start_end_index(self, x):
        """
        Finds the start and end indecies when a 1D array crosses zero
        """
        x0 = x[:-1]
        x1 = x[1:]
        x_change_sign = (x0*x1 < 0)   
        start_idx = np.nonzero(x_change_sign & (x0 < x1))[0]  # Find the start of an overpass
        end_idx = np.nonzero(x_change_sign & (x0 > x1))[0]    # Find the end of an overpass
        return start_idx, end_idx

    # def brightness(self, idx, sun_rho):
    #     """
    #     Compute the brightness magnitude of the satellite
    #     """
    #     assert self.sun is not None
    #     # find phase angle between observer -- satellite -- sun
    #     gamma = self.el[idx] - sun_el[idx]

    def find_overpasses(self, min_elevation=10, store_sat_id=False, sunset_el=-6):
        start_idx, end_idx = self._start_end_index(self.el - min_elevation)
        num_overpasses = min(start_idx.size, end_idx.size)       # Iterate over start/end indecies and gather inbetween indecies
        if start_idx.size < end_idx.size:
            end_idx = end_idx[1:]
        sat_overpasses = [None] * num_overpasses
        for j in range(num_overpasses):
            # Store indecies of overpasses in a list
            idx0 = start_idx[j]
            idxf = end_idx[j]
            idxmax = np.argmax(self.el[idx0:idxf+1])
            start_pt = self.point(idx0)
            max_pt = self.point(idx0 + idxmax)
            end_pt = self.point(idxf)

            # Find visible start and end times
            if self.sun is not None:
                sun_rho = self.sun.rECEF[:,idx0:idxf+1] - self.rsiteECEF
                sun_sez = ecef2sez(sun_rho, self.location.lat, self.location.lon)
                sun_rng = np.linalg.norm(sun_sez, axis=0)
                sun_el = np.arcsin(sun_sez[2] / sun_rng) * RAD2DEG
                site_in_sunset = sun_el - sunset_el < 0
                site_in_sunset_idx = np.nonzero(site_in_sunset)[0]
                if site_in_sunset_idx.size == 0:
                    passtype = PassType.daylight # site is always sunlit, so overpass is in daylight
                else:
                    # get satellite illumination values for this overpass
                    sat_visible = (self.sat.illuminated[idx0:idxf+1] * site_in_sunset)
                    if np.any(sat_visible):
                        sat_visible_idx = np.nonzero(sat_visible)[0]
                        sat_visible_start_idx = sat_visible_idx.min()
                        sat_visible_end_idx = sat_visible_idx.max()
                        vis_start_pt = self.point(idx0 + sat_visible_start_idx)
                        vis_end_pt = self.point(idx0 + sat_visible_end_idx)
                        passtype = PassType.visible # site in night, sat is illuminated
                        brightness_idx = np.argmax(self.el[idx0 + sat_visible_start_idx: idx0 + sat_visible_end_idx + 1])
                        sat_rho = self.rECEF[:, idx0 + sat_visible_start_idx + brightness_idx]
                        sat_rng = self.rng[idx0 + sat_visible_start_idx + brightness_idx]
                        sun_rho_b = sun_rho[:, sat_visible_start_idx + brightness_idx]
                        sun_rng_b = sun_rng[sat_visible_start_idx + brightness_idx]
                        sat_site_sun_angle = math.acos(  
                            np.dot(sat_rho, sun_rho_b) / (sat_rng * sun_rng_b)
                        )
                        beta = math.pi - sat_site_sun_angle  # phase angle: site -- sat -- sun angle
                        sat_intrinsic_mag = -1.8  # for ISS
                        brightness = sat_intrinsic_mag - 15 + 5*math.log10(sat_rng) - 2.5*math.log10(math.sin(beta) + (math.pi - beta)*math.cos(beta))
                    else:
                        passtype = PassType.unlit  # nighttime, not illuminated (radio night)
            else:
                passtype = None
            overpass_dict = {
                'start_pt': start_pt,
                'max_pt': max_pt,
                'end_pt': end_pt,
            }
            if store_sat_id:
                overpass_dict['satellite_id'] = self.sat.id
            if (passtype is not None) and (passtype == PassType.visible):
                overpass_dict['vis_start_pt'] = vis_start_pt
                overpass_dict['vis_end_pt'] = vis_end_pt
                overpass_dict['brightness'] = round(brightness, 3)
            overpass_dict['type'] = passtype
            overpass = Overpass.construct(**overpass_dict)
            sat_overpasses[j] = overpass
        return sat_overpasses


# class SatelliteRV():
#     __slots__ = ['satellite','tle','rsun','datetime','julian_date','rECEF',
#                  'rECI','latitude','longitude','altitude','visible']
#     def __init__(self):
#         self.satellite = None
#         self.tle = None
#         self.rsun = None
#         self.datetime = None
#         self.julian_date = None
#         self.rECEF = None
#         self.rECI = None
#         self.latitude = None
#         self.longitude = None
#         self.altitude = None
#         self.visible = None



