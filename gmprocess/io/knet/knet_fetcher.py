# stdlib imports
from datetime import datetime, timedelta
import re
from collections import OrderedDict
import tempfile
import os.path
import tarfile
import glob
import shutil

# third party imports
import pytz
import numpy as np
import requests
from bs4 import BeautifulSoup
from openquake.hazardlib.geo.geodetic import geodetic_distance
from obspy.core.utcdatetime import UTCDateTime

# local imports
from gmprocess.io.fetcher import DataFetcher
from gmprocess.io.knet.core import read_knet
from gmprocess.streamcollection import StreamCollection


JST_OFFSET = 9 * 3600  # Japan standard time is UTC + 9
SEARCH_URL = 'http://www.kyoshin.bosai.go.jp/cgi-bin/kyoshin/quick/list_eqid_en.cgi?1+YEAR+QUARTER'
RETRIEVE_URL = 'http://www.kyoshin.bosai.go.jp/cgi-bin/kyoshin/auth/makearc'

# http://www.kyoshin.bosai.go.jp/cgi-bin/kyoshin/auth/makearc?formattype=A&eqidlist=20180330081700%2C20180330000145%2C20180330081728%2C1%2C%2Fkyoshin%2Fpubdata%2Fall%2F1comp%2F2018%2F03%2F20180330081700%2F20180330081700.all_acmap.png%2C%2Fkyoshin%2Fpubdata%2Fknet%2F1comp%2F2018%2F03%2F20180330081700%2F20180330081700.knt_acmap.png%2C%2Fkyoshin%2Fpubdata%2Fkik%2F1comp%2F2018%2F03%2F20180330081700%2F20180330081700.kik_acmap.png%2CHPRL&datanames=20180330081700%3Balldata&datakind=all

CGIPARAMS = OrderedDict()
CGIPARAMS['formattype'] = 'A'
CGIPARAMS['eqidlist'] = ''
CGIPARAMS['datanames'] = ''
CGIPARAMS['alldata'] = None
CGIPARAMS['datakind'] = 'all'

QUARTERS = {1: 1, 2: 1, 3: 1,
            4: 4, 5: 4, 6: 4,
            7: 7, 8: 7, 9: 7,
            10: 10, 11: 10, 12: 10}

# 2019/03/13-13:48:00.00
TIMEPAT = '[0-9]{4}/[0-9]{2}/[0-9]{2}-[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{2}'
LATPAT = '[0-9]{2}\.[0-9]{2}N'
LONPAT = '[0-9]{3}\.[0-9]{2}E'
DEPPAT = '[0-9]{3}km'
MAGPAT = 'M[0-9]{1}\.[0-9]{1}'
TIMEFMT = '%Y/%m/%d-%H:%M:%S.%f'


class KNETFetcher(DataFetcher):
    def __init__(self, time, lat, lon,
                 depth, magnitude,
                 user=None, password=None,
                 radius=100, dt=16, ddepth=30,
                 dmag=0.3,
                 rawdir=None, ):
        """Create a KNETFetcher instance.

        Download KNET/KikNet data from the Japanese NIED site:
        http://www.kyoshin.bosai.go.jp/cgi-bin/kyoshin/quick/list_eqid_en.cgi

        Args:
            time (datetime): Origin time.
            lat (float): Origin latitude.
            lon (float): Origin longitude.
            depth (float): Origin depth.
            magnitude (float): Origin magnitude.
            user (str): username for KNET/KikNET site.
            password (str): (Optional) password for site.
            radius (float): Search radius (km).
            dt (float): Search time window (sec).
            ddepth (float): Search depth window (km).
            dmag (float): Search magnitude window (magnitude units).
            rawdir (str): Path to location where raw data will be stored.
                          If not specified, raw data will be deleted.
        """
        # for knet/kiknet, username/password is required
        if user is None or password is None:
            raise Exception(
                'Username/password are required to retrieve KNET/KikNET data.')
        self.user = user
        self.password = password
        tz = pytz.UTC
        self.time = tz.localize(time)
        self.lat = lat
        self.lon = lon
        self.radius = radius
        self.dt = dt
        self.rawdir = rawdir
        self.depth = depth
        self.magnitude = magnitude
        self.ddepth = ddepth
        self.dmag = dmag
        self.jptime = self.time + timedelta(seconds=JST_OFFSET)

    def getMatchingEvents(self, solve=True):
        """Return a list of dictionaries matching input parameters.

        Args:
            solve (bool): 
                If set to True, then this method 
                should return a list with a maximum of one event.

        Returns:
            list: List of event dictionaries, with fields:
                  - time Event time (UTC)
                  - lat Event latitude
                  - lon Event longitude
                  - depth Event depth
                  - mag Event magnitude
        """
        jpyear = str(self.jptime.year)
        jpquarter = str(QUARTERS[self.jptime.month])
        url = SEARCH_URL.replace('YEAR', jpyear)
        url = url.replace('QUARTER', jpquarter)
        req = requests.get(url)
        data = req.text
        soup = BeautifulSoup(data, features="lxml")
        select = soup.find('select')
        options = select.find_all('option')
        times = []
        lats = []
        lons = []
        depths = []
        mags = []
        values = []
        for option in options:
            eventstr = option.contents[0]
            timestr = re.search(TIMEPAT, eventstr).group()
            latstr = re.search(LATPAT, eventstr).group()
            lonstr = re.search(LONPAT, eventstr).group()
            depstr = re.search(DEPPAT, eventstr).group()
            magstr = re.search(MAGPAT, eventstr).group()
            lat = float(latstr.replace('N', ''))
            lon = float(lonstr.replace('E', ''))
            depth = float(depstr.replace('km', ''))
            mag = float(magstr.replace('M', ''))
            etime = datetime.strptime(timestr, TIMEFMT)
            times.append(np.datetime64(etime))
            lats.append(lat)
            lons.append(lon)
            depths.append(depth)
            mags.append(mag)
            values.append(option.get('value'))

        times = np.array(times)
        lats = np.array(lats)
        lons = np.array(lons)
        depths = np.array(depths)
        mags = np.array(mags)
        distances = geodetic_distance(self.lon, self.lat, lons, lats)
        didx = distances <= self.radius
        jptime = np.datetime64(self.jptime)
        # dtimes is in microseconds
        dtimes = np.abs(jptime - times)
        tidx = dtimes <= np.timedelta64(int(self.dt), 's')
        etimes = times[didx & tidx]
        elats = lats[didx & tidx]
        elons = lons[didx & tidx]
        edepths = depths[didx & tidx]
        emags = mags[didx & tidx]
        events = []
        for etime, elat, elon, edep, emag, evalue in zip(etimes, elats,
                                                         elons, edepths,
                                                         emags, values):
            jtime = UTCDateTime(str(etime))
            utime = jtime - JST_OFFSET
            edict = {'time': utime,
                     'lat': elat,
                     'lon': elon,
                     'depth': edep,
                     'mag': emag,
                     'cgi_value': evalue}
            events.append(edict)

        if solve and len(events) > 1:
            event = self.solveEvents(events)
            events = [event]

        return events

    def retrieveData(self, event_dict):
        """Retrieve data from NIED, turn into StreamCollection.

        Args:
            event (dict):
                Best dictionary matching input event, fields as above
                in return of getMatchingEvents().

        Returns:
            StreamCollection: StreamCollection object.
        """
        rawdir = self.rawdir
        if self.rawdir is None:
            rawdir = tempfile.mkdtemp()

        cgi_value = event_dict['cgi_value']
        firstid = cgi_value.split(',')[0]

        url = RETRIEVE_URL
        payload = {'formattype': ['A'],
                   'eqidlist': cgi_value,
                   'datanames': '%s;alldata' % firstid,
                   'datakind': ['all']}
        req = requests.get(url, params=payload,
                           auth=(self.user, self.password))
        dtime = event_dict['time']
        fname = dtime.strftime('%Y%m%d%H%M%S') + '.tar'
        localfile = os.path.join(rawdir, fname)
        if req.status_code == 200:
            with open(localfile, 'wb') as f:
                for chunk in req:
                    f.write(chunk)

        # open the tarball, extract the kiknet/knet gzipped tarballs
        tar = tarfile.open(localfile)
        names = tar.getnames()
        tarballs = []
        for name in names:
            if 'img' in name:
                continue
            ppath = os.path.join(rawdir, name)
            tarballs.append(ppath)
            tar.extract(name, path=rawdir)
        tar.close()

        # remove the tar file we downloaded
        os.remove(localfile)

        subdirs = []
        for tarball in tarballs:
            tar = tarfile.open(tarball, mode='r:gz')
            if 'kik' in tarball:
                subdir = os.path.join(rawdir, 'kiknet')
            else:
                subdir = os.path.join(rawdir, 'knet')
            subdirs.append(subdir)
            tar.extractall(path=subdir)
            tar.close()
            os.remove(tarball)

        streams = []
        for subdir in subdirs:
            gzfiles = glob.glob(os.path.join(subdir, '*.gz'))
            for gzfile in gzfiles:
                os.remove(gzfile)
            datafiles = glob.glob(os.path.join(subdir, '*.*'))
            for dfile in datafiles:
                print(dfile)
                streams += read_knet(dfile)

        if self.rawdir is None:
            shutil.rmtree(rawdir)

        stream_collection = StreamCollection(streams=streams)
        return stream_collection
