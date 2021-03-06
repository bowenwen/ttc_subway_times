import logging, logging.config
import sys
import requests #to handle http requests to the API 
import aiohttp  # this lib replaces requests for asynchronous i/o
import asyncio
import async_timeout
from psycopg2 import connect #Connect to local PostgreSQL db
from datetime import datetime
from time import sleep
#import socket

# Note - the package yarl, a dependency of aiohttp, breaks the library on version 0.9.4 and 0.9.5
# So need to manually install 0.9.3 or try 0.9.6, which should fix the bug.
# e.g. pip uninstall yarl; pip install yarl==0.9.3
# see https://github.com/KeepSafe/aiohttp/issues/1635
# Use virtualenv to avoid borking your system install of python

# optimal performance may require installation of cchardet and aiodns packages
# see  http://aiohttp.readthedocs.io/en/stable/index.html

#Not used anymore
class MissingDataException( TypeError):
    pass

class TTCSubwayScraper( object ):
    LINES = {1: range(1, 33), #max value must be 1 greater
             2: range(33, 64),
             4: range(64, 69)}
    BASE_URL = "http://www.ttc.ca/Subway/loadNtas.action"
    #BASE_URL = 'http://www.ttc.ca/Subway/'
   
    NTAS_SQL = """INSERT INTO public.ntas_data(\
            requestid, id, station_char, subwayline, system_message_type, \
            timint, traindirection, trainid, train_message) \
            VALUES (%(requestid)s, %(id)s, %(station_char)s, %(subwayline)s, %(system_message_type)s, \
            %(timint)s, %(traindirection)s, %(trainid)s, %(train_message)s);
          """
    def __init__(self, logger, con):
        self.logger = logger
        self.con = con
        
    def get_API_response(self, line_id, station_id):
        payload = {"subwayLine":line_id,
                   "stationId":station_id,
                   "searchCriteria":''}
        
        # with timeout set to 10, need to use a try block here to catch timeout errors
        try:
            r = requests.get(self.BASE_URL, params = payload, timeout = 10)
        except requests.exceptions.RequestException as err:
            self.logger.critical(err)
            return None
        
        # another try block will check for http error codes
        try:
            r.raise_for_status()
        except requests.exceptions.RequestException as err:
            self.logger.critical(err)
            return None

        return r.json()

    def insert_request_info(self, poll_id, data, line_id, station_id, request_date):
        request_row = {}
        request_row['pollid'] = poll_id
        request_row['request_date'] = str(request_date)
        request_row['data_'] = data['data']
        request_row['stationid'] = station_id
        request_row['lineid'] = line_id
        request_row['all_stations'] = data['allStations']
        request_row['create_date'] = data['ntasData'][0]['createDate'].replace('T', ' ')
        cursor = self.con.cursor()
        cursor.execute("INSERT INTO public.requests(data_, stationid, lineid, all_stations, create_date, pollid, request_date)"
                       "VALUES(%(data_)s, %(stationid)s, %(lineid)s, %(all_stations)s, %(create_date)s, %(pollid)s, %(request_date)s)"
                       "RETURNING requestid", request_row)
        request_id = cursor.fetchone()[0] 
        self.con.commit()
        cursor.close()
        self.logger.debug("Request " + str(request_id) + ": " + str(request_row) )

        return request_id

    def insert_ntas_data(self, ntas_data, request_id):
        cursor = self.con.cursor()

        for record in ntas_data:
            record_row ={}
            record_row['requestid'] = request_id
            record_row['id'] = record['id']
            record_row['station_char'] = record['stationId']
            record_row['subwayline'] = record['subwayLine']
            record_row['system_message_type'] = record['systemMessageType']
            record_row['timint'] = record['timeInt']
            record_row['traindirection'] = record['trainDirection']
            record_row['trainid'] = record['trainId']
            record_row['train_message'] = record['trainMessage']
            if record_row['train_message'] == "Arriving":
                continue # skip any records that are Arriving or not final
            cursor.execute(self.NTAS_SQL, record_row)
        self.con.commit()
        cursor.close()

    def insert_poll_start(self, time):
        cursor = self.con.cursor()
        cursor.execute("INSERT INTO public.polls(poll_start)"
                        "VALUES(%s)"
                        "RETURNING pollid", (str(time),))
        poll_id = cursor.fetchone()[0]
        self.con.commit()
        cursor.close()
        self.logger.debug("Poll " + str(poll_id) + " started at " + str(time) )
        return poll_id

    def update_poll_end(self, poll_id, time):
        cursor = self.con.cursor()
        cursor.execute("UPDATE public.polls set poll_end = %s"
                        "WHERE pollid = %s", (str(time), str(poll_id)) )
        self.con.commit()
        cursor.close()
        self.logger.debug("Poll " + str(poll_id) + " ended at " + str(time) )


    def check_for_missing_data( self, stationid, lineid, data):
        if data is None:
            return True
        ntasData = data.get('ntasData')
        if ntasData is None or ntasData ==[] :
            return True

        # there is data, so do more careful checks

        # at interchange stations, the API returns trains on both lines, despite the fact that each line has a unique stationid
        # So for interchange stations, make sure we have at least one observation on the right line!
        interchanges = (9,10,22,30,47,48,50,64) 
        # if we're not in an interchange station, we're done
        if stationid not in interchanges:
            return False 
        # most general way to detect the problem is to check subwayLine field
        linecodes = ("YUS", "BD", "", "SHEP")
        # look for one train observed in the right direction
        for record in ntasData:
            if record['subwayLine'] == linecodes[lineid-1]:
                return False

        # none match
        return True
    

    async def query_station_async(self, session, line_id, station_id ):
        retries = 4
        payload = {"subwayLine":line_id,
                       "stationId":station_id,
                       "searchCriteria":''}
        for attempt in range(retries):
            #with async_timeout.timeout(10):
            try:
                rtime = datetime.now()
                async with session.get(self.BASE_URL, params=payload, timeout=5) as resp:
                    #data = None
                    data = await resp.json()

                    if self.check_for_missing_data(station_id, line_id, data):
                        self.logger.debug("Missing data!")
                        self.logger.debug("Try " + str(attempt+1) + " for station " + str(station_id) + " failed.")
                        if attempt < retries-1:
                            self.logger.debug("Sleeping 2s  ...")
                            await asyncio.sleep(2)
                        continue
                    return (data, rtime)
            except Exception as err:
                self.logger.critical(err)
                self.logger.debug("request error!")
                self.logger.debug("Try " + str(attempt+1) + " for station " + str(station_id) + " failed.")
                if attempt < retries-1:
                    self.logger.debug("Sleeping 2s  ...")
                    await asyncio.sleep(2)
                continue
        return (None, None)


    # async def qtest( self, session, line_id, station_id):
    #     payload = {"subwayLine":line_id,
    #                    "stationId":station_id,
    #                    "searchCriteria":''}
    #     async with session.get(self.BASE_URL, params=payload, timeout=10) as resp:
    #         ret = await resp.json()
    #         print(ret)
    #         return ret

    # async def qstest( self, loop):
    #       async with aiohttp.ClientSession() as session:
    #             tasks = []
    #             task = asyncio.ensure_future(self.query_station_async(session, 1, 1))
    #             tasks.append(task)
    #             responses = await asyncio.gather(*tasks)
    #             if self.check_for_missing_data( 1, 1, responses[0]) :
    #                 errmsg = 'No data for line {line}, station {station}'
    #                 self.logger.error(errmsg.format(line=1, station=1))
    #                 self.logger.debug( errmsg.format(line=1, station=1) )
    #             print( responses[0])

    async def query_all_stations_async(self,loop):
            poll_id = self.insert_poll_start(datetime.now())

            # run requests simultaneously using asyncio
            async with aiohttp.ClientSession() as session:
                tasks = []
                for line_id, stations in self.LINES.items():
                    for station_id in stations:
                        task = asyncio.ensure_future(self.query_station_async(session, line_id, station_id))
                        tasks.append(task)
                responses = await asyncio.gather(*tasks)

            # check results and insert into db
            for line_id, stations in self.LINES.items():
                for station_id in stations:
                    (data, rtime) = responses[station_id-1]  # may want to tweak this to check error codes etc
                    if self.check_for_missing_data( station_id, line_id, data) :
                        errmsg = 'No data for line {line}, station {station}'
                        self.logger.error(errmsg.format(line=line_id, station=station_id))
                        self.logger.debug( errmsg.format(line=line_id, station=station_id) )
                        continue    
                    request_id = self.insert_request_info(poll_id, data, line_id, station_id, rtime )
                    self.insert_ntas_data(data['ntasData'], request_id)
        
            self.update_poll_end( poll_id, datetime.now() )




    def query_all_stations(self):
        poll_id = self.insert_poll_start( datetime.now() )
        retries = 3
        for line_id, stations in self.LINES.items():
            for station_id in stations:
                for attempt in range(retries):
                    data = self.get_API_response(line_id, station_id)
                    if not self.check_for_missing_data( station_id, line_id, data) :
                        break
                    else:
                        self.logger.debug("Try " + str(attempt+1) + " for station " + str(station_id) + " failed.")
                        #if data is None:
                        # for http and timeout errors, sleep 2s before retrying
                        self.logger.debug("Sleeping 2s  ...")
                        sleep(2)
       

                if self.check_for_missing_data( station_id, line_id, data) :
                    errmsg = 'No data for line {line}, station {station}'
                    self.logger.error(errmsg.format(line=line_id, station=station_id))
                    self.logger.debug( errmsg.format(line=line_id, station=station_id) )
                    continue    
                request_id = self.insert_request_info(poll_id, data, line_id, station_id, datetime.now() )
                self.insert_ntas_data(data['ntasData'], request_id)

        self.update_poll_end( poll_id, datetime.now() )

if __name__ == '__main__':
    
    import configparser
    CONFIG = configparser.ConfigParser(interpolation=None)
    CONFIG.read('db.cfg')
    dbset = CONFIG['DBSETTINGS']
    
    LOGGING = CONFIG['LOGGING']
    
#    LOGGING = {
#        'version':1,
#        'formatters' : {
#        'f': {'format':
#              '%(asctime)s %(name)-12s %(levelname)-8s %(message)s'}
#        },
#        'handlers' : {
#            'h': {'class': 'logging.StreamHandler',
#                  'formatter': 'f',
#                  'level': logging.DEBUG}
#        },
#        'root' : {
#            'handlers': ['h'],
#            'level': logging.DEBUG
#        }
#    }
    
    logging.basicConfig(level=logging.getLevelName(LOGGING['level']), format=LOGGING['format'], filename=LOGGING['filename'])
    
    LOGGER = logging.getLogger(__name__)
    
    # add console output for debugging
    if LOGGING['level'] == 'DEBUG':
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        ch.setFormatter(formatter)
        LOGGER.addHandler(ch)
    
    

    try:
        con = connect(**dbset)  
        scraper = TTCSubwayScraper(LOGGER, con)

        # old synchronous i/o version
        #scraper.query_all_stations()

        # new asynchronous i/o version
        loop = asyncio.get_event_loop()
        future = asyncio.ensure_future( scraper.query_all_stations_async(loop))
        #future = asyncio.ensure_future( scraper.qstest(loop))
        loop.run_until_complete( future )


        con.close()
    except Exception as err:
        LOGGER.critical("Unhandled exception - quitting.")
        LOGGER.critical(err)
