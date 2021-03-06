import socket
import threading
import networking
import time
from time import gmtime, strftime
import sys, getopt
import logging
import urllib2
import signal # Just to ensure that KeyboardInterrupt exception goes to main thread and no one else. See Python threading caveats on docs.python.org
import subprocess
import os
import re

LOGFILE = "log.txt"
LATENCYFILE = "latency.txt"
LISTEN_PORT = 52334
SEEDS = "seeds.txt"
MEMBERS = "members.txt"
COORDINATOR_CHECK_INTERVAL = 5 # 5 seconds
Coordinator = ''
NAMELEN_LEN = 2
HTTP_PORT = 9090
PING_RETRIES = 2
PONG_RETRIES = 2
PROBE_INTERVAL = 5
NotReceivedCoordinatorUpdate = True
latency = {}

def stringOfMembers():
    """Returns a space separated all member string"""
    memString = '['
    f = open(MEMBERS, 'r')
    for line in f:
        line = line.strip()
        memString = memString + " " + line 

    memString = memString + "]"
    f.close()
    return memString

def distributeMembersFile(members):
    for node in members:
        node = node.strip() # Strip trailing \n
        if node == Coordinator: # Coordinator doesn't need to send the file to itself
            continue
        conn = None
        try:
            conn = networking.getConnection(node, LISTEN_PORT)
            # TODO: Can possibly update the list of members here as well
            networking.perfectSend('DWLD', conn)
            networking.perfectReceive(4, conn)
        except Exception: # Desperate hack as except socket.error was somehow not catching "Connection refused" exceptions
             exctype, value = sys.exc_info()[:2]
             logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
 
             logging.critical(node + " missed out on an updated copy of members file")
        finally:
            if conn:
                networking.closeConnection(conn)

def listMembers():
    "List all members in the overlay network as known to the coordinator"
    members = []
    f = open(MEMBERS, 'r')
    for line in f:
        line = line.strip()
        members.append(line)
    f.close()
    return members



def addMember(joiner):
    "Add nodeId to the list of active members in the overlay"
    # Update the list of active members
    f = open(MEMBERS, 'a')
    f.write(joiner + '\n') # FIXME: Implement locking on file
    f.close()
    # Maintain a global data structure of all members ??

    # Send updated list to all active members
    # TODO: Possibly acquire a lock on file, so that all nodes get the same file
    members = listMembers()
    distributeMembersFile(members)
            
def removeMembers(nodeIdList):
    "Remove nodeId from the list of active members in the overlay"
    # Update the list of active members
    members = listMembers()
    f = open(MEMBERS, 'w')
    for node in members:
        node = node.strip()
        if node not in nodeIdList:
            f.write(node + '\n')
        else:
            logging.debug("Removing disconnected node " + node + " from members.txt file")
    f.close()
    # Send updated list to all active members
    members = listMembers()
    distributeMembersFile(members)

def rewriteMembersFile(members):
    logging.debug("Chosen to be the Coordinator. Rewriting members file")
    f = open(MEMBERS, 'w')
    for node in members:
        f.write(node + '\n')
    f.close()

def sendLatencyMeasurements(coordinatorCopy):
    """ Send latency measurements to the Coordinator"""
    logging.debug("sendLatencyMeasurements() begins")
    me = socket.gethostname()
    conn = None
    try:
        conn = networking.getConnection(coordinatorCopy, LISTEN_PORT)

        numberOfEntries = len(latency)
        stringNumberOfEntries = ''
        if numberOfEntries < 10:
           stringNumberOfEntries = '0' + str(numberOfEntries) 
        else:
            stringNumberOfEntries = str(numberOfEntries)

        networking.perfectSend('LTNC', conn)
        networking.perfectSend(stringNumberOfEntries, conn)

        for node, value in latency.iteritems():
            measurement = "[" + me + ", " + node + ", " + str(value) + "]"
            networking.perfectSend(str(len(measurement)), conn)
            networking.perfectSend(measurement, conn)

        logging.debug("Printing latency dictionary:" + str(latency))


    except Exception:
        exctype, value = sys.exc_info()[:2]
        logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
        logging.debug("Failed in sending some or all latency measurements to " + coordinatorCopy)
    finally:
        if conn:
            networking.closeConnection(conn)

# Reference/Credits: User Ignacio Verona's question on Stackoverflow "Parsing ping output with Python"
def senseLatency():
    """Measure latency between this node and other members in the overlay"""
    logging.debug("senseLatency() begins")
    members = listMembers()
    me = socket.gethostname()

    for node in members:
        node = node.strip()
        if node == me:
            continue
        else:
            output = os.popen('ping ' + node + " -c 5 -q  | egrep \"packet loss|rtt\"").read()
            match = re.search('([\d]*\.[\d]*)/([\d]*\.[\d]*)/([\d]*\.[\d]*)/([\d]*\.[\d]*)', output)

            if match:
                a = 0.9
                ping_min = match.group(1)
                ping_avg = match.group(2)
                ping_max = match.group(3)
                if node not in latency:
                   # print ping_avg
                   latency[node] = ping_avg 
                else:
                    prev = float(latency[node])
                    latency[node] = 0.9*prev
                    latency[node] = latency[node] + (0.1)*float(ping_avg)

                tstamp = strftime("%Y-%m-%d %H:%M:%S", gmtime())
                readingString = "[" + me + ", " + node + ", " + str(ping_avg) + ", " + str(latency[node]) + " , " + tstamp + "]\n"
                f = open(LATENCYFILE, 'a')
                f.write(readingString)
                f.close()
            else:
                logging.debug("No regex match found")

            # match = re.search('(\d*)% packet loss', output)
            # pkt_loss = match.group(1)

def electCoordinator():
    """Handles the node behavior when coordinator failure has been detected"""
    logging.debug("electCoordinator() begins")
    global Coordinator
    global NotReceivedCoordinatorUpdate
    activeMembers = listMembers()
    activeMembers = sorted(activeMembers)
    logging.debug(str(activeMembers))

    me = socket.gethostname()
    while NotReceivedCoordinatorUpdate:
        logging.debug("Loop begins: " + str(activeMembers))
        potentialActiveMembers = activeMembers
        # This node is the highest ranked node as known to this node. 
        # Become the Coordinator and announce it to everyone else
        if me == activeMembers[0]:
            Coordinator = me
            logging.debug("Declared myself as the Coordinator: " + Coordinator)
            for node in potentialActiveMembers:
                node = node.strip()
                if node == Coordinator:
                    continue
                else:
                    conn = None
                    try:
                        conn = networking.getConnection(node, LISTEN_PORT)
                        networking.perfectSend('NEWC', conn)
                        networking.perfectSend(str(len(me)), conn)
                        networking.perfectSend(me, conn)
                        networking.perfectReceive(4, conn)
                    except Exception: # Bare except!!
                        exctype, value = sys.exc_info()[:2]
                        logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
 
                        activeMembers.remove(node)
                        logging.debug("electCoordinator(): Removing " + node + " from the list of activeMembers")
                        logging.debug(str(activeMembers))
                    finally:
                        if conn:
                            networking.closeConnection(conn)

            rewriteMembersFile(activeMembers)
            distributeMembersFile(activeMembers)
            break
        else:
            # Don't try to become a Coordinator until any of the higher ranked nodes is alive.
            # Until then, just probe each of the higher ranked nodes
            for node in potentialActiveMembers:
                node = node.strip()
                if node == me:
                    break
                else:
                    logging.debug("Sending LKUP to " + node)
                    conn = None
                    try:
                        conn = networking.getConnection(node, LISTEN_PORT)
                        networking.perfectSend('LKUP', conn) # Check if the node at other end is alive
                        response = networking.perfectReceive(4, conn)
                        if response == 'RPLY':
                            nameLen = int(networking.perfectReceive(2, conn))
                            Coordinator = networking.perfectReceive(nameLen, conn)
                            return # Can return safely. Found Coordinator
                        elif response == 'NONE':
                            pass

                    except Exception: # FIXME: 'Bare' except is considered bad
                        exctype, value = sys.exc_info()[:2]
                        logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
                        logging.debug("Before removing " + node + ":" + str(activeMembers))
                        activeMembers.remove(node)
                        logging.debug("electCoordinator(): Removing " + node + " from the list of activeMembers")
                        logging.debug(str(activeMembers))
                    finally:
                        if conn:
                            networking.closeConnection(conn)
              
    NotReceivedCoordinatorUpdate = True # For subsequent Coordinator Failures


def sendPingToAllMembers():
    "Check if active members listed in members.txt file are still connected"
    members = listMembers()
    disconnectedMembers = []
    for node in members:
        node = node.strip()
        if node == Coordinator:
            continue
        res = networking.ping(node, PING_RETRIES)
        if res != 0: # Probe lost
           logging.debug("NODE disconnected: " + node)
           disconnectedMembers.append(node)

    return disconnectedMembers

class pingUtil(threading.Thread):
    def __init__(self, threadId, name, isCoordinator):
        threading.Thread.__init__(self)
        self.threadId = threadId
        self.name = name
        self.isCoordinator = isCoordinator # Boolean value indicating whether this thread is coordinator or not


    def run(self):
         global Coordinator
         
         while 1:
             if self.isCoordinator == True:
                 disconnMembers = sendPingToAllMembers()
                 if disconnMembers:
                     removeMembers(disconnMembers)

                 # Meanwhile if a higher ranked node detects timeout of the coordinator, 
                 # it may announce itself as the coordinator in a NEWC message
                 if Coordinator != socket.gethostname(): 
                     self.isCoordinator = False

                 # time.sleep(PROBE_INTERVAL)
             else:
                 try:
                     networking.pong(PONG_RETRIES) #FIXME: Should not be blocking. What if this becomes a coordinator while blocking on PING
                 except socket.timeout, socket.error:
                     Coordinator = ''
                     electCoordinator() # Blocks this thread until a new coordinator has been decided. And that is a good thing
                     logging.info("electCoordinator() returned with new Coordinator as " + Coordinator)
                     if Coordinator == socket.gethostname():
                         self.isCoordinator = True
                         logging.debug("This node will send ping to other members")
                     else:
                         self.isCoordinator = False
                         logging.debug("This node will reply to ping from the Coordinator")

def join(Coordinator):
    """Called to join the overlay commanded by the Coordinator"""
    conn = None
    try:
        conn = networking.getConnection(Coordinator, LISTEN_PORT)
        networking.perfectSend('JOIN', conn)
        me = socket.gethostname()
        nameLen = str(len(me)) # FIXME: name length should be expressible in 2 digits (Node name of length 100 or 9 will screw things up!)
        networking.perfectSend(nameLen, conn)
        networking.perfectSend(me, conn)
        networking.closeConnection(conn) # TODO: Possibly use this connection to download updated members file
    except Exception:
        exctype, value = sys.exc_info()[:2]
        logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
        if conn:
            networking.closeConnection(conn)
        raise



class handleMessage(threading.Thread):
    def __init__(self, (clientSocket, address)):
        threading.Thread.__init__(self)
        self.clientSocket = clientSocket
        self.address = address

    def run(self):
        global Coordinator
        global NotReceivedCoordinatorUpdate
        MSGTYPE_LEN = 4
        msgType = ''
        try:
            msgType = networking.perfectReceive(MSGTYPE_LEN, self.clientSocket)
        except Exception:
            exctype, value = sys.exc_info()[:2]
            logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
            if self.clientSocket:
                networking.closeConnection(self.clientSocket)
            return
            
            # msgType is either JOIN, LEAV OR LOOK (Coordinator Lookup)
        # Following two characters denote length of nodename
        if msgType == 'JOIN' or msgType == 'LEAV': # FIXME: Only Coordinator should receive such message
            try:
                logging.info("Received " + msgType + " message")
                nameLen = int(networking.perfectReceive(NAMELEN_LEN, self.clientSocket))  # NAMELEN_HDR is 2 characters. Example: 25
                nodeName = networking.perfectReceive(nameLen, self.clientSocket)
                if msgType == 'JOIN':
                    addMember(nodeName)
                    memString = stringOfMembers()
                    logging.info("\n[EVENT JOIN]: " + nodeName + "\n[MEMBERS]: " + memString)
                else:
                    removeMembers(nodeName)
                    memString = stringOfMembers()
                    logging.info("\n[EVENT LEAVE]: " + nodeName + "\n[MEMBERS]: " + memString)
            except Exception:
                exctype, value = sys.exc_info()[:2]
                logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
            finally:
                if self.clientSocket:
                    networking.closeConnection(self.clientSocket)


        elif msgType == 'LKUP':
            logging.debug("Received LKUP message")
            try:
                if Coordinator != '':
                    networking.perfectSend('RPLY', self.clientSocket)
                    networking.perfectSend(str(len(Coordinator)), self.clientSocket)
                    networking.perfectSend(Coordinator, self.clientSocket)
                else:
                    networking.perfectSend('NONE', self.clientSocket)
            except Exception:
                exctype, value = sys.exc_info()[:2]
                logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
            finally:
                if self.clientSocket:
                    networking.closeConnection(self.clientSocket)

        elif msgType == 'DWLD':
            logging.debug("Received indication to download members.txt file")
            try:
                f = urllib2.urlopen('http://' + Coordinator + ':' + str(HTTP_PORT) + '/' + MEMBERS) # FIXME: Does this require closing?
                localMembersFile = open(MEMBERS, 'w')
                localMembersFile.write(f.read())
                localMembersFile.close()
                memString = stringOfMembers()
                logging.info("\n[COORDINATOR]: " + Coordinator + "\n[UPDATE LOCAL MEMBERS FILE]: \n[MEMBERS]: " + memString)
                networking.perfectSend('DONE', self.clientSocket)
            except Exception: # Bare except!!
                exctype, value = sys.exc_info()[:2]
                logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
                logging.critical("Could not download members file from " + Coordinator)
            finally:
                if self.clientSocket:
                    networking.closeConnection(self.clientSocket)
             
        elif msgType == 'NEWC': # New coordinator has announced its arrival
            try:
                nameLen = int(networking.perfectReceive(NAMELEN_LEN, self.clientSocket))
                Coordinator = networking.perfectReceive(nameLen, self.clientSocket)
                networking.perfectSend('UPDC', self.clientSocket)
                NotReceivedCoordinatorUpdate = False # Breaks the loop of electCoordinator() function
                logging.info("Received New Coordinator [NEWC] announcement from " + Coordinator)
            except Exception:
                exctype, value = sys.exc_info()[:2]
                logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
                logging.debug("Failed to receive the new Coordinator [NEWC] announcement")
                Coordinator = ''
                NotReceivedCoordinatorUpdate = True
            finally:
                if self.clientSocket:
                    networking.closeConnection(self.clientSocket)

        elif msgType == 'LTNC':
            logging.debug("Member sending latency measurements")
            try:
                numberOfEntries = int(networking.perfectReceive(2, self.clientSocket))
                for i in range(numberOfEntries):
                    measurementLen = int(networking.perfectReceive(2, self.clientSocket))
                    measurement = networking.perfectReceive(measurementLen, self.clientSocket)
                    logging.debug(measurement) # Only logging for time being

            except Exception:
                exctype, value = sys.exc_info()[:2]
                logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
                logging.debug("Did not receive all latency measurements from the member")
            finally:
                if self.clientSocket:
                    networking.closeConnection(self.clientSocket)


        elif msgType == 'CHCK': # Message from a member checking if higher ranked nodes are still alive
            logging.debug("Received Check Alive [CHCK] message from a member")
            try:
                networking.perfectSend('LIVE', self.clientSocket)
            finally:
                networking.closeConnection(self.clientSocket)

class listener(threading.Thread):
    def __init__(self, threadId, name, port):
            threading.Thread.__init__(self)
            self.threadId = threadId
            self.name = name
            self.port = port
            self.err = 0

    def run(self):
        serverSocket = None
        try:
            serverSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            serverSocket.bind(('', self.port))
            serverSocket.listen(5)
            childThreads = []
        except Exception:
            exctype, value = sys.exc_info()[:2]
            logging.debug("General exception: " + str(exctype) + " Value: " + str(value))

            if serverSocket:
                serverSocket.close()
            self.err = value 
            sys.exit() # Equivalent to calling thread.exit(). See Python caveats at docs.python.org/2/library/thread.html

        while 1:
            clientSocket, address = serverSocket.accept()
            # logging.debug("Got new connection req from a member")
            servingThread = handleMessage((clientSocket, address))
            servingThread.daemon = True
            servingThread.start()
            childThreads.append(servingThread)
            
def coordinatorLookup():
    global Coordinator # Reference the global variable (Very Important)
    f = open(SEEDS, 'r')
    me = socket.gethostname()
    for x in range (1,2):
        for node in f:
            node = node.strip() # Remove leading and trailing whitespace
            if node != me:
                logging.debug("Checking if " + node + " knows the coordinator")
                conn = None
                try:
                    conn = networking.getConnection(node, LISTEN_PORT)
                    networking.perfectSend('LKUP', conn)
                    response = networking.perfectReceive(4, conn) # response can be None as well
                    if response == 'RPLY':
                        nameLen = int(networking.perfectReceive(2, conn))
                        Coordinator = networking.perfectReceive(nameLen, conn)
                        logging.debug("Received RPLY " + Coordinator + " in coordinatorLookup()")
                    elif response == 'NONE':
                        logging.debug(node + " doesn't know about the coordinator")
                except Exception:
                    exctype, value = sys.exc_info()[:2]
                    logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
                    Coordinator = '' # If we got error, the node we were talking wasn't a good candidate for coordinator anyway
                finally:
                    if conn:
                        networking.closeConnection(conn)
        
            # print "Coordinator :" + Coordinator
            if Coordinator != '':
                logging.debug("Found Coordinator")
                break
        
        if Coordinator == '':
            # Retry to query for  coordinator after some interval
            time.sleep(COORDINATOR_CHECK_INTERVAL)
    
    if Coordinator == '':
        Coordinator = me
        
    logging.info("coordinatorLookup returning [COORDINATOR]: " + Coordinator)
    return Coordinator

def bootstrap(startAsCoordinator):
    "Members bootstrapping looking for an overlay and coordinator"
    global Coordinator
    messageHandler = listener(1, "Listener for JOIN, LEAV, LKUP, DWLD", LISTEN_PORT)
    messageHandler.daemon = True # Will be killed automatically (and abruptly) when main thread exits
    messageHandler.start()
    isCoordinator = startAsCoordinator
    if startAsCoordinator == False:
        logging.debug("Starting life as a normal member")

        while Coordinator == '':
            # Do not declare the result of coordinatorLookup() as coordinator until this node joins the overlay
            canBeCoordinator = coordinatorLookup() 
            logging.debug("Coordinator lookup returned: " + canBeCoordinator)
            if canBeCoordinator != socket.gethostname():
                try:
                    join(canBeCoordinator)
                except Exception:
                    exctype, value = sys.exc_info()[:2]
                    logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
                    logging.debug("Joining the Coordinator " + Coordinator + " failed. Retry Coordinator Lookup")
            else:
                isCoordinator = True
                Coordinator = canBeCoordinator

    return messageHandler, isCoordinator, Coordinator

def main(argv):
    startAsCoordinator = False
    logLevel = logging.INFO # Default logging level

    f = open(MEMBERS, 'w') # Node creates an empty members file
    f.write(socket.gethostname() + '\n')
    f.close()

    try:
        opts, args = getopt.getopt(argv, "cmvh")
    except getopt.GetoptError:
        print 'Usage: python member.py -cmvh'
        sys.exit(2)

    for opt, arg in opts:
        if opt == '-c':
            startAsCoordinator = True
        elif opt == '-m':
            startAsCoordinator = False
        elif opt == '-v':
            logLevel = logging.DEBUG

    logging.basicConfig(filename="log.txt", level = logLevel, format = '%(asctime)s %(levelname)s %(message)s')
    logging.info("\n========== NEW EXPERIMENT STARTS ==========\n\n")
    global Coordinator
    messageHandler, isCoordinator, Coordinator = bootstrap(startAsCoordinator)
    
    pingHandler = pingUtil(2, "Ping handler thread", isCoordinator)
    pingHandler.daemon = True
    pingHandler.start()
    do_exit = False
    interval = 0

    while do_exit == False:
        try:
            if messageHandler.err != 0:
                do_exit = True

            senseLatency()
            interval = interval + 1
            if interval == 10:
                if Coordinator != socket.gethostname() and Coordinator != '':
                    sendLatencyMeasurements(Coordinator)
                    interval = 0

            time.sleep(30)
        except KeyboardInterrupt:
            print "Ctrl-C caught. Exit would terminate daemon threads..."
            do_exit = True

    # Stop running threads
    # messageHandler.stop()
    if isCoordinator == False and Coordinator != '':
        try:
            conn = networking.getConnection(Coordinator, LISTEN_PORT)
            networking.perfectSend('LEAV', conn)
            me = socket.gethostname()
            nameLen = len(me)
            networking.perfectSend(str(nameLen), conn)
            networking.perfectSend(me, conn)
            logging.debug("Sent LEAV message to " + Coordinator)
            networking.closeConnection(conn)
        except Exception: 
            exctype, value = sys.exc_info()[:2]
            logging.debug("General exception: " + str(exctype) + " Value: " + str(value))
            logging.debug("Failed to send LEAV message to " + Coordinator)

    sys.exit(0)

if __name__ == "__main__":
    main(sys.argv[1:])
 
