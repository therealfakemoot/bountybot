"""
Created on 2015-05-11

@author: Valtyr Farshield
"""

import time
import urllib2
import sqlite3 as lite
import gzip
import json
import threading
import re

from epicenter import Epicenter
from StringIO import StringIO
from bountyconfig import BountyConfig
from evescout.evescout import EveScout
from tripwire.tripwire_sql import TripwireSql


class Zkb():
    """
    Zkillboard Handling Class
    """

    @staticmethod
    def lastkill(solarSystemID, limit = 1):
        headers = {
            "User-Agent": BountyConfig.USER_AGENT,
            "Accept-encoding": "gzip"
        }
        url = "https://zkillboard.com/api/solarSystemID/{}/limit/{}/".format(solarSystemID, limit)
        
        try:
            request = urllib2.Request(url, None, headers)
            response = urllib2.urlopen(request)
        except urllib2.URLError as e:
            print "[Error]", e.reason
        else:
            if response.info().get("Content-Encoding") == "gzip":
                buf = StringIO(response.read())
                f = gzip.GzipFile(fileobj=buf)
                data = f.read()
            else:
                data = response.read()
            
            # try to parse JSON received from server
            try:
                parsed_json = json.loads(data)
            except ValueError as e:
                print "[Error]", e
            else:
                if len(parsed_json) > 0:
                    return [str(parsed_json[0]['killID']), parsed_json[0]['killTime']]
        
        return None

# data structure for a wormhole system
class Wormhole():
    def __init__(self, sysId, name, whclass, date, comments, lastkillId, lastkillDate, watchlist):
        self.sysId = sysId                # internal Eve Id of system [private]
        self.name = name                  # name of the wormhole (ex. J123450)
        self.whclass = whclass            # wormhole class
        self.date = date                  # creation date
        self.comments = comments          # person of contact, links, etc.
        self.lastkillId = lastkillId      # last kill Id in the system [private]
        self.lastkillDate = lastkillDate  # last kill date in the system
        self.watchlist = watchlist        # should bountybot report kills in system? True/False
        
    def __str__(self):
        return "*{}* [C{}] - Created: {}, Watchlist: {}, LastKill: {}, Info: *{}*".format(
            self.name,
            self.whclass,
            self.date,
            self.watchlist,
            self.lastkillDate,
            self.comments
        )

class GenericWh():
    def __init__(self, idx, date, description, jcodes):
        self.idx = idx
        self.date = date
        self.description = description
        self.jcodes = jcodes
        
    def __str__(self):
        return "Generic *#{}* [{}] {}".format(self.idx, self.date, self.description)
    
# Bounty Bot main class
class BountyDb():
    def __init__(
            self,
            db_epicenter,
            db_name,
            table_jcodes,
            table_generics,
            report_kill,
            report_thera,
            report_thera_generic,
            report_thera_tripnull,
            interval,
            apiwait,
            cyclelimit
    ):
        # initialize instance variables
        self.__db_epicenter = db_epicenter                    # Epicenter database name
        self.__db_name = db_name                              # SQLite database name
        self.__table_jcodes = table_jcodes                    # SQLite jcodes table name
        self.__table_generics = table_generics                # SQLite generics table name
        self.__report_kill = report_kill                      # callback report function for kill detection
        self.__report_thera = report_thera                    # callback report function for Thera connection
        self.__report_thera_generic = report_thera_generic    # callback report function for Thera connection
        self.__report_thera_tripnull = report_thera_tripnull  # callback report function for Thera connection
        self.__interval = interval                            # period (seconds) of the __check() function
        self.__apiwait = apiwait                              # wait time between Zkillboard api calls
        self.__cyclelimit = cyclelimit                        # limit cycle ugly hack ;)
        self.__cycle = 0                                      # cycle counter init to 0
        
        self.__whlist = []          # wormhole list
        self.__generics = []        # generics list
        self.__thera_recent = {}    # thera recent specific reports
        self.__thera_generic = {}   # thera recent generic reports
        self.__thera_tripnull = {}  # thera recent tripnull reports
        
        # create Epicenter instance
        self.__epi = Epicenter(self.__db_epicenter, "wormholes", "statics")
        
        # database handling
        self.__db_con = lite.connect(self.__db_name)
        self.__cursor = self.__db_con.cursor()
        
        # create generic wormholes table
        self.__cursor.execute("""CREATE TABLE IF NOT EXISTS {}
            (Idx INTEGER PRIMARY KEY AUTOINCREMENT,
            Date TEXT,
            Description TEXT)""".format(self.__table_generics))
        
        #create jcode table
        self.__cursor.execute("""CREATE TABLE IF NOT EXISTS {}
            (SysId INTEGER PRIMARY KEY,
            Name TEXT,
            Date TEXT,
            Comments TEXT,
            LastkillId TEXT,
            LastkillDate TEXT,
            Watchlist INTEGER)""".format(self.__table_jcodes))
        
        # fetch values from the database (if any)
        print "-- Database contents:"
        print "Table '{}':".format(self.__table_generics)
        for row in self.__cursor.execute("SELECT * FROM {} ORDER BY Idx ASC".format(self.__table_generics)):
            [result_info, jcodes] = self.__epi.computeGeneric(row[2])
            self.__generics.append(GenericWh(row[0], row[1], row[2], jcodes))
            print row
            print result_info
        
        print ""
        
        print "Table '{}':".format(self.__table_jcodes)
        for row in self.__cursor.execute("SELECT * FROM {} ORDER BY Name ASC".format(self.__table_jcodes)):
            print row
            if int(row[6] > 0):
                watchlist = True
            else:
                watchlist = False
            self.__whlist.append(Wormhole(row[0], row[1], self.__epi.getClass(row[1]), row[2], row[3], row[4], row[5], watchlist))
        print "--"
        print ""
        
        # begin checking for kills if enabled
        if BountyConfig.REPORTS_ACTIVE:
            print "[Info] Bounty Bot manager loaded - check at every {} seconds".format(self.__interval)
            self.__start_check()
    
    # check if the input parameter is a valid wormhole (found in Epicenter database)
    def valid_wormhole(self, name):
        name = name.upper()  # ignore case
        sysId = self.__epi.getSysId(name)
        
        if sysId > 0:
            return True
        else:
            return False
    
    # get overall information on a wormhole
    def info_jcode(self, name):
        name = name.upper()                # ignore case
        sysId = self.__epi.getSysId(name)  # retrieve the solar system Id from Epicenter Database
        
        # only if wormhole is in the database
        if sysId > 0:
            message = self.__epi.info(name) + "\n"
            message += self.__epi.planets(name)
        else:
            message = "Unknown wormhole name '{}'".format(name.upper())
            
        return message
    
    # get overall information on a wormhole
    def compact_info_jcode(self, name):
        name = name.upper()                # ignore case
        sysId = self.__epi.getSysId(name)  # retrieve the solar system Id from Epicenter Database
        
        # only if wormhole is in the database
        if sysId > 0:
            message = self.__epi.info(name) + ", " + self.__epi.planets(name, display_compact=True)
        else:
            message = "Unknown wormhole name '{}'".format(name.upper())
            
        return message

    @staticmethod
    def shortlink(message):
        """
        Finds URLs and correctly formats them for Slack and Tripire
        :param message: Input message from Slack
        :return: Two processed strings (Slack and Tripwire)
        """
        bb_message = trip_message = message
        regex = re.compile('(<(http[s]?://.*?)(?:\|(.*?))?>)', re.IGNORECASE)
        for group, link, shortlink in regex.findall(message):
            bb_message = bb_message.replace(group, shortlink if shortlink else link, 1)
            trip_message = trip_message.replace(
                group,
                '<a href="{}" target="_blank">{}</a>'.format(link, shortlink if shortlink else link),
                1
            )
        return [bb_message, trip_message]

    # add a new wormhole (if valid)
    def add_jcode(self, name, watchlist, comments):
        name = name.upper()                # ignore case
        sysId = self.__epi.getSysId(name)  # retrieve the solar system Id from Epicenter Database
        
        # check if system name is a valid Wormhole
        if sysId > 0:
            # make sure the system isn't already in the whlist
            if self.get_jcode(name) == None:
                # fetch Zkillboard data and check if anything was received
                zkbInfo = Zkb.lastkill(sysId)
                if zkbInfo:
                    [lastkillId, lastkillDate] = zkbInfo
                else:
                    lastkillId = 1
                    lastkillDate = '2016-01-01 00:00:00'

                # all information is available, proceed to database addition
                [bb_comments, trip_comments] = self.shortlink(comments)
                creation_date = time.strftime("%Y-%m-%d")
                whclass = self.__epi.getClass(name)
                wh = Wormhole(sysId, name, whclass, creation_date, bb_comments, lastkillId, lastkillDate, watchlist)
                self.__whlist.append(wh)

                # add tripwire comments
                if BountyConfig.TRIP_INFO["enabled"]:
                    tripwire_thread = threading.Thread(target=self.tripwire_add_or_update, args=(sysId, trip_comments))
                    tripwire_thread.daemon = True
                    tripwire_thread.start()

                # database insert
                statement = "INSERT INTO {} VALUES (?, ?, ?, ?, ?, ?, ?)".format(self.__table_jcodes)
                self.__cursor.execute(
                    statement,
                    (sysId, name, creation_date, bb_comments, lastkillId, lastkillDate, 1 if watchlist else 0)
                )
                self.__db_con.commit()
                return str(wh)  # all OK :)
            else:
                return "{} - already in the list".format(name)
        else:
            return "{} - not a valid wormhole".format(name)
    
    # add a new generic wormhole, ex: C3 with HS static
    def add_generic(self, description):
        creation_date = time.strftime("%Y-%m-%d")
        [bb_description, trip_description] = self.shortlink(description)

        # database insert
        statement = "INSERT INTO {} VALUES (NULL, ?, ?)".format(self.__table_generics)
        self.__cursor.execute(statement, (creation_date, bb_description))
        idx = self.__cursor.lastrowid
        self.__db_con.commit()
        
        # list insert
        [result_info, jcodes] = self.__epi.computeGeneric(bb_description)
        generic_wh = GenericWh(idx, creation_date, bb_description, jcodes)
        self.__generics.append(generic_wh)

        # add tripwire comments
        if BountyConfig.TRIP_INFO["enabled"]:
            tripwire_thread = threading.Thread(target=self.tripwire_add_generic, args=(idx, trip_description, jcodes))
            tripwire_thread.daemon = True
            tripwire_thread.start()

        return [str(generic_wh), result_info]
    
    # remove wormhole (if exists)
    def remove_jcode(self, name):
        name = name.upper()  # ignore case
        wh = self.get_jcode(name)

        # was it found?
        if wh != None:
            sysId = wh.sysId
            self.__whlist.remove(wh)
            
            # database remove
            statement = "DELETE FROM {} WHERE Name=?".format(self.__table_jcodes)
            self.__cursor.execute(statement, (name,))
            self.__db_con.commit()

            # delete tripwire comments
            if BountyConfig.TRIP_INFO["enabled"]:
                tripwire_thread = threading.Thread(target=self.tripwire_delete, args=(sysId,))
                tripwire_thread.daemon = True
                tripwire_thread.start()
            
            return "Wormhole {} removed".format(name)
        else:
            return "Wormhole {} is not in the list".format(name)
    
    # remove generic wormhole by Idx (if exists)
    def remove_generic(self, idx):
        for generic_wh in self.__generics:
            if generic_wh.idx == idx:
                self.__generics.remove(generic_wh)
                
                # database remove
                statement = "DELETE FROM {} WHERE Idx=?".format(self.__table_generics)
                self.__cursor.execute(statement, (idx, ))
                self.__db_con.commit()

                # delete tripwire comments
                if BountyConfig.TRIP_INFO["enabled"]:
                    tripwire_thread = threading.Thread(
                        target=self.tripwire_delete_generic,
                        args=(idx, generic_wh.jcodes)
                    )
                    tripwire_thread.daemon = True
                    tripwire_thread.start()
                
                return "Generic wormhole Id#{} removed".format(idx)
        
        return "Generic wormhole Id#{} is not in the list".format(idx)
    
    # edit the comments of a specific wormhole
    def edit_jcode(self, name, watchlist, comments):
        name = name.upper()  # ignore case
        
        for index, wh in enumerate(self.__whlist):
            if wh.name == name:
                wh.watchlist = watchlist

                # only update comments if input string is not empty
                if len(comments) > 0:
                    [bb_comments, trip_comments] = self.shortlink(comments)
                    wh.comments = bb_comments
                    statement = "UPDATE {} SET Watchlist=?, Comments=? WHERE Name=?".format(self.__table_jcodes)
                    self.__cursor.execute(statement, (1 if watchlist else 0, bb_comments, name))

                    # edit tripwire comments
                    if BountyConfig.TRIP_INFO["enabled"]:
                        tripwire_thread = threading.Thread(
                            target=self.tripwire_add_or_update,
                            args=(wh.sysId, trip_comments)
                        )
                        tripwire_thread.daemon = True
                        tripwire_thread.start()
                else:
                    statement = "UPDATE {} SET Watchlist=? WHERE Name=?".format(self.__table_jcodes)
                    self.__cursor.execute(statement, (1 if watchlist else 0, name))

                self.__whlist[index] = wh
                self.__db_con.commit()
                return str(wh)

        return "Wormhole {} is not in the list".format(name)
    
    # edit the description of a generic wormhole
    def edit_generic(self, idx, description):
        for index, generic_wh in enumerate(self.__generics):
            if generic_wh.idx == idx:
                [bb_description, trip_description] = self.shortlink(description)
                [result_info, jcodes] = self.__epi.computeGeneric(bb_description)
                generic_wh.description = bb_description
                old_jcodes = list(generic_wh.jcodes)
                generic_wh.jcodes = jcodes
                self.__generics[index] = generic_wh
        
                # database modify
                statement = "UPDATE {} SET Description=? WHERE Idx=?".format(self.__table_generics)
                self.__cursor.execute(statement, (bb_description, idx))
                self.__db_con.commit()

                # edit tripwire comments
                if BountyConfig.TRIP_INFO["enabled"]:
                    tripwire_thread = threading.Thread(
                        target=self.tripwire_update_generic,
                        args=(idx, trip_description, old_jcodes, jcodes)
                    )
                    tripwire_thread.daemon = True
                    tripwire_thread.start()
                
                return [str(generic_wh), result_info]

        return ["Generic #{} is not in the list".format(idx), ""]
    
    # returns the list of wormholes in the whlist
    def list_jcode(self):
        return sorted(self.__whlist, key=lambda x: x.name, reverse=False)  # sort by jcode ascending
    
    # returns the list of generic wormholes
    def list_generic(self):
        return self.__generics
    
    # returns the list of J-codes associated with generic of specified ID
    def generic_jcodes(self, idx):
        for generic_wh in self.__generics:
            if generic_wh.idx == idx:
                return generic_wh.jcodes
        return None
    
    # wrapper for Epicenter's search function
    def search_generic(self, description):
        return self.__epi.computeGeneric(description)
    
    # wrapper for Epicenter's static code information function
    def search_static(self, static_code):
        return self.__epi.getStatic(static_code)

    # wrapper for Epicenter's static mass information function
    def static_mass(self, static_code):
        return self.__epi.static_mass(static_code)
    
    # get information on a specific wormhole (if present in whlist)
    def get_jcode(self, name):
        name = name.upper()  # ignore case
        
        # only search if list is not empty
        if len(self.__whlist) > 0:
            for wh in self.__whlist:
                if wh.name == name:
                    return wh
        
        # return None if system hasn't been found
        return None
    
    # checks if the specified wormhole is in the generic order list
    def verify_generic(self, name):
        name = name.upper()  # ignore case
        
        match_list = []
        for generic_wh in self.__generics:
            if name in generic_wh.jcodes:
                match_list.append(generic_wh.idx)
        
        return match_list
    
    # clear the entire jcode list
    def clear_jcode(self):
        self.__whlist = []
        
        # database remove all
        self.__cursor.execute("DELETE FROM {}".format(self.__table_jcodes))
        self.__db_con.commit()

    # clear the entire generic wormhole list
    def clear_generic(self):
        self.__generics = []
        
        # database remove all
        self.__cursor.execute("DELETE FROM {}".format(self.__table_generics))
        self.__db_con.commit()

    # -----------------------------------------------------------------------------
    @staticmethod
    def tripwire_connect():
        return TripwireSql(
            user=BountyConfig.TRIP_INFO["user"],
            passwd=BountyConfig.TRIP_INFO["pass"],
            mask=BountyConfig.TRIP_INFO["mask"],
            trip_char_id=BountyConfig.TRIP_INFO["trip_char_id"],
            host=BountyConfig.TRIP_INFO["host"],
            port=BountyConfig.TRIP_INFO["port"],
            db=BountyConfig.TRIP_INFO["db"]
        )

    def tripwire_add_or_update(self, sysId, comments):
        trip_sql = self.tripwire_connect()
        trip_sql.add_or_update_specific(sysId, comments)
        trip_sql.close_db()

    def tripwire_delete(self, sysId):
        trip_sql = self.tripwire_connect()
        trip_sql.delete_specific(sysId)
        trip_sql.close_db()

    def tripwire_add_generic(self, generic_id, description, jcodes):
        system_ids = [self.__epi.getSysId(name) for name in jcodes]
        trip_sql = self.tripwire_connect()
        trip_sql.add_generic(generic_id, description, system_ids)
        trip_sql.close_db()

    def tripwire_update_generic(self, generic_id, description, old_jcodes, new_jcodes):
        old_system_ids = [self.__epi.getSysId(name) for name in old_jcodes]
        new_system_ids = [self.__epi.getSysId(name) for name in new_jcodes]
        trip_sql = self.tripwire_connect()
        trip_sql.delete_generic(generic_id, old_system_ids)
        trip_sql.add_generic(generic_id, description, new_system_ids)
        trip_sql.close_db()

    def tripwire_delete_generic(self, generic_id, jcodes):
        system_ids = [self.__epi.getSysId(name) for name in jcodes]
        trip_sql = self.tripwire_connect()
        trip_sql.delete_generic(generic_id, system_ids)
        trip_sql.close_db()
    # -----------------------------------------------------------------------------

    # sqlite db can not be updated from 2 different threads
    def __update_sqlite(self, db_name, table_name, lastkillId, lastkillDate, wh_name):
        conn = lite.connect(db_name)
        c = conn.cursor()
        statement = "UPDATE {} SET LastkillId=?, LastkillDate=? WHERE Name=?".format(table_name)
        c.execute(statement, (lastkillId, lastkillDate, wh_name))
        conn.commit()
        conn.close()
        
    # update wormhole list for thread safety purposes
    def __update_whlist(self, lastkillId, lastkillDate, wh_name):
        for index, wh in enumerate(self.__whlist):
            # is the wormhole still in the list?
            if wh_name == wh.name:
                wh.lastkillId = lastkillId
                wh.lastkillDate = lastkillDate
                self.__whlist[index] = wh
    
    # thread start helper function
    def __start_check(self):
        bounty_thread = threading.Timer(1, self.__check, ())
        bounty_thread.setDaemon(True)
        bounty_thread.start()
    
    # check for every system if the last killId is different from the stored killId
    def __check(self):
        print "[{}] Checking cycle {}...".format(time.strftime("%Y-%m-%d %H:%M:%S"), str(self.__cycle + 1))
        threading.Timer(self.__interval, self.__check, ()).start()
        check_counter = 0

        # populate list with wormhole connections from Thera (if enabled)
        if BountyConfig.THERA:
            thera_systems = EveScout.thera_connections()
            print "Retrieving Thera connections: {}".format(thera_systems)
        else:
            thera_systems = []

        # delete old Thera specific reports
        for key, value in self.__thera_recent.items():
            if int(time.time()) - value > BountyConfig.THERA_HOURS * 3600:
                del self.__thera_recent[key]

        # delete old Thera generic reports
        for key, value in self.__thera_generic.items():
            if int(time.time()) - value > BountyConfig.THERA_HOURS * 3600:
                del self.__thera_generic[key]

        # delete old Thera tripnull reports
        for key, value in self.__thera_tripnull.items():
            if int(time.time()) - value > BountyConfig.THERA_HOURS * 3600:
                del self.__thera_tripnull[key]

        # check Thera generics
        for th_sys in thera_systems:
            for generic_wh in list(self.__generics):
                if th_sys in generic_wh.jcodes and th_sys not in self.__thera_generic.keys():
                    self.__thera_generic[th_sys] = int(time.time())
                    self.__report_thera_generic(generic_wh, th_sys)

        # check Thera tripnulls
        for th_sys in thera_systems:
            match_obj = re.search("J000[0-9]{3}", th_sys)
            if match_obj and th_sys not in self.__thera_tripnull.keys():
                self.__thera_tripnull[th_sys] = int(time.time())
                self.__report_thera_tripnull(th_sys)

        # make new list for thread safety reasons and check that list
        for wh in list(self.__whlist):
            # only check watchlisted wormholes
            if wh.watchlist:

                # check for Thera connections
                if wh.name in thera_systems:
                    if wh.name not in self.__thera_recent.keys():
                        self.__thera_recent[wh.name] = int(time.time())
                        self.__report_thera(wh)

                # fetch Zkillboard data and check if anything was received
                time.sleep(self.__apiwait)
                zkbInfo = Zkb.lastkill(wh.sysId, self.__cycle + 1)
                
                if zkbInfo != None:
                    check_counter += 1
                    [lastkillId, lastkillDate] = zkbInfo                    
                    if int(lastkillId) > int(wh.lastkillId):
                        # update wormhole list and database (if it wasn't removed from watchlist in the meantime)
                        self.__update_whlist(lastkillId, lastkillDate, wh.name)
                        self.__update_sqlite(self.__db_name, self.__table_jcodes, lastkillId, lastkillDate, wh.name)
                        
                        # finally, report kill, hurray! :)
                        print "[Report] {} - Kill detected at {}, Id: {}".format(wh.name, lastkillDate, lastkillId)
                        self.__report_kill(wh)
                else:
                    print "[Error] Zkillboard API call failed"
        
        print "[Info] Cycle ended - {} wormholes were checked".format(check_counter)
        
        # super ugly hack for limit cycling (to bypass mean zkb caching) >:)
        self.__cycle += 1
        if self.__cycle >= self.__cyclelimit:
            self.__cycle = 0

def print2screen(msg):
    print "[Report]: ", msg

def main():
    # Development purposes
    bb = BountyDb("epicenter.db", "bounties.db", "wormholes", "generics", print2screen, 600, 3, 7)

if __name__ == '__main__':
    main()
