"""A separate consumer reads the delivered paper fields and tries boarding at both stops."""
from contextlib import closing
import json
import sqlite3

# Independent fixture issuer's boarding records for the service day. HH:MM format.
LEDGER = {
    "O4": ("Orchard", "18:44", "Mill", "19:08"),
    "H5": ("Mill", "19:15", "Hill", "19:49"),
    "H8": ("Mill", "19:23", "Hill", "19:58"),
    "H7": ("Mill", "19:21", "Hill", "19:52"),
}

def minutes(t):
    h,m = map(int,t.split(":"))
    return 60*h+m

def paper(root,number):
    with closing(sqlite3.connect(root/"desk.db")) as db:
        recipient,body=db.execute("SELECT recipient,body FROM delivered WHERE id=?", (number,)).fetchone()
        return recipient, json.loads(body)

def travel(body,minimum,issued):
    route=body["route"]
    a,b=route["first"],route["second"]
    if not {a,b}<=set(issued): return "carrier has no departure"
    for code,start,leave,end,reach in [
        (a,route["origin"],route["origin_departure"],route["interchange"],route["interchange_arrival"]),
        (b,route["interchange"],route["interchange_departure"],route["destination"],route["destination_arrival"])]:
        name,t1,target,t2=LEDGER[code]
        if (start,leave,end,reach)!=(name,minutes(t1),target,minutes(t2)):
            return "paper does not match boarding records"
    if route["origin"]!="Orchard" or route["destination"]!="Hill": return "wrong journey"
    if route["interchange_departure"]-route["interchange_arrival"]<minimum: return "missed connection"
    return "arrived at "+route["destination"]

def typeset(body):
    # Print the actual schedule data handed to this receiver, ignoring internal run state.
    rows=[]
    for s in body["schedules"]:
        start,t1,end,t2=LEDGER[s["code"]]
        if (s["origin"],s["departure"],s["destination"],s["arrival"])!=(start,minutes(t1),end,minutes(t2)):
            raise ValueError("Timetable mismatch")
        rows.append(f"{s['code']}  {start} {t1}  →  {end} {t2}")
    return rows
