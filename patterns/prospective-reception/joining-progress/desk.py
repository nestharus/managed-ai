"""Two timetables, one passenger. Python 3.11+; trusted local fixture."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

SCHEMA = """
CREATE TABLE request (previous INTEGER, minimum INTEGER, reason TEXT);
CREATE TABLE work (owner TEXT PRIMARY KEY, revision INTEGER, run TEXT, state TEXT);
CREATE TABLE service (code TEXT PRIMARY KEY, owner TEXT, origin TEXT, destination TEXT, departure INTEGER, arrival INTEGER);
CREATE INDEX by_owner_departure ON service(owner,departure);
CREATE TABLE amendment (id TEXT PRIMARY KEY, owner TEXT, basis INTEGER, code TEXT, departure INTEGER, arrival INTEGER, status TEXT);
CREATE INDEX by_amendment_owner ON amendment(owner,status);
CREATE TABLE report (id TEXT PRIMARY KEY, account TEXT, state TEXT);
CREATE TABLE event (seq INTEGER PRIMARY KEY, body TEXT);
CREATE TABLE delivered (id INTEGER PRIMARY KEY, recipient TEXT, body TEXT);
"""

def event(db, body):
    db.execute("INSERT INTO event(body) VALUES(?)", (body,))

def time_str(minutes):
    return "%02d:%02d" % divmod(minutes, 60)

def init(root, noise=40, at="interrupted"):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "desk.db"
    if path.exists():
        raise ValueError("This desk already exists. Use another directory.")
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO request VALUES(NULL,5,'Initial request')")
        for code, owner, start, end, dep, arr in [
            ("O4","Orchard","Orchard","Mill",1124,1148), ("H5","Hill","Mill","Hill",1155,1189)]:
            db.execute("INSERT INTO work VALUES(?,1,?,'ended')", (owner,owner+"-10"))
            db.execute("INSERT INTO service VALUES(?,?,?,?,?,?)",(code,owner,start,end,dep,arr))
            event(db, f"{owner} published timetable 1. Print run {owner}-10 ended.")
        db.execute("INSERT INTO amendment VALUES('A27','Hill',1,'H8',1163,1198,'offered')")
        event(db,"Hill dispatched an addendum to ended run Hill-10. Owner offer A27 permits publishing H8 at 19:23.")
        db.execute("INSERT INTO report VALUES('N9','A caller expects an H7 at 19:21. Hill has not issued it.','unconfirmed')")
        event(db, "N9 radio report received.")
        if at in ("interrupted","retained"):
            change(db,12 if at=="interrupted" else 5)
        for n in range(noise):
            event(db, f"Day shift filed inspection {n+1:03d}.")

def change(db, minimum):
    if not 1 <= minimum <= 60:
        raise ValueError("Receiver minimum must be between 1 and 60 minutes.")
    db.execute("UPDATE request SET previous=minimum, minimum=?, reason='Passenger updated the transfer allowance.'",(minimum,))
    event(db,f"Passenger requested a minimum {minimum}-minute connection.")

class Desk:
    def __init__(self, db):
        self.db=db
        self.reads=[]
        self.steps=0

    def step(self):
        self.steps+=1
        return 0

    def get(self, label, sql, args=()):
        self.steps=0
        self.db.set_progress_handler(self.step,1)
        try:
            rows=[dict(row) for row in self.db.execute(sql,args)]
        finally:
            self.db.set_progress_handler(None,0)
        self.reads.append(dict(source=label, rows=len(rows), returned_bytes=len(json.dumps(rows).encode()), vm_steps=self.steps))
        return rows

    def request(self):
        return self.get("request","SELECT * FROM request")[0]

    def owners(self):
        return self.get("operator editions","SELECT * FROM work ORDER BY owner")

    def services(self):
        return self.get("issued departures","SELECT * FROM service ORDER BY departure")

    def amendments(self):
        return self.get("Hill amendment desk","SELECT a.*,w.revision,w.run FROM amendment a JOIN work w "
                        "ON w.owner=a.owner WHERE a.owner='Hill' AND a.status='offered' ORDER BY a.id")

    def reports(self):
        return self.get("radio reports","SELECT id,account,state FROM report ORDER BY id")

    def pair(self,m):
        return self.get("eligible connection","SELECT a.code first,b.code second,a.origin origin,a.departure origin_departure,a.destination interchange,"
                        "a.arrival interchange_arrival,b.destination destination,"
                        "b.departure interchange_departure,b.arrival destination_arrival FROM service a "
                        "JOIN service b ON a.destination=b.origin "
                        "WHERE a.origin='Orchard' AND b.destination='Hill' AND b.departure-a.arrival>=? "
                        "ORDER BY b.arrival,a.departure LIMIT 1",(m,))

    def history(self,offset):
        if offset<0: raise ValueError("History offset must be nonnegative.")
        return self.get("activity page","SELECT seq,body FROM event ORDER BY seq DESC LIMIT 5 OFFSET ?",(offset,))

    def activity(self,recipient):
        runs=self.owners(); total=self.get("activity count","SELECT count(*) AS n FROM event")[0]["n"]
        lines=[f"{recipient.capitalize()} desk | Activity", f"{sum(r['state']=='ended' for r in runs)}/{len(runs)} printing runs ended | {total} events"]
        lines.extend(f"{r['seq']}. {r['body']}" for r in self.history(0))
        return lines+["Inspect an owner or read an earlier page with history OFFSET."]

    def progress(self,recipient):
        req=self.request()
        if recipient=="printer":
            lines=["Printer | Individual timetables", *(f"{r['owner']} timetable {r['revision']} available | {r['run']} {r['state']}" for r in self.owners()),
                   "Print the current operator timetables: print."]
        else:
            lines=["Passenger | Orchard → Hill via Mill"]
            if req["previous"] is None or req["previous"]==req["minimum"]:
                lines.append(f"Requested transfer allowance: {req['minimum']} min.")
            else:
                lines.append(f"Transfer allowance changed from {req['previous']} to {req['minimum']} min.")
            options=self.pair(req["minimum"])
            if options:
                p=options[0]
                lines.append(f"{p['first']} + {p['second']} affords {p['interchange_departure']-p['interchange_arrival']} min. Ready to issue: issue.")
            else:
                closest=self.pair(0)
                available=(f"{closest[0]['first']} + {closest[0]['second']} affords {closest[0]['interchange_departure']-closest[0]['interchange_arrival']} min."
                           if closest else "No ordered connection is issued.")
                lines.append("No itinerary meets the request. "+available)
                offers=[r for r in self.amendments() if r["basis"]==r["revision"]]
                for a in offers:
                    candidates=self.get("offered transfer", "SELECT ?-arrival AS wait FROM service "
                                        "WHERE origin='Orchard' AND destination='Mill' AND arrival<=? "
                                        "ORDER BY arrival LIMIT 1", (a["departure"],a["departure"]))
                    if candidates:
                        wait=candidates[0]["wait"]
                        if wait>=req["minimum"]:
                            lines.append(f"{a['code']} would allow {wait} min. {a['owner']} permits adding it "
                                         f"on ended {a['run']}: amend {a['id']}.")
                        else:
                            lines.append(f"Offered {a['code']} would allow at most {wait} min; still short.")
            lines.extend(r["account"] for r in self.reports() if r["state"]=="unconfirmed")
            lines.append("Individual timetables can still be printed.")
        receipts=self.get("latest receipt", "SELECT id FROM delivered WHERE recipient=? ORDER BY id DESC LIMIT 1",(recipient,))
        if receipts:
            lines.append(f"Prior {recipient} receipt {receipts[0]['id']} is available for inspection.")
        return lines

    def inspect(self,topic):
        return {"request":self.request,"services":self.services,"owners":self.owners,"amendments":self.amendments,
                "reports":self.reports, "receipts":lambda:self.get("issued receipts","SELECT * FROM delivered ORDER BY id")}[topic]()

    def amend(self,ident):
        rows=self.get("amendment admission","SELECT a.*, w.revision FROM amendment a JOIN work w ON a.owner=w.owner WHERE a.id=?",(ident,))
        if not rows: return dict(status="refused",reason="Unknown amendment.")
        a=rows[0]
        if a['status']=="applied": return dict(status="already applied",service=a["code"])
        if a["basis"]!=a["revision"] or a["status"]!="offered": return dict(status="refused",reason="Offer withdrawn or based on an earlier timetable.")
        self.db.execute("INSERT INTO service VALUES(?,?,'Mill','Hill',?,?)",(a['code'],a['owner'],a['departure'],a['arrival']))
        self.db.execute("UPDATE work SET revision=revision+1 WHERE owner=?",(a['owner'],))
        self.db.execute("UPDATE amendment SET status='applied' WHERE id=?",(ident,))
        event(self.db,f"{a['owner']} admitted {ident}; {a['code']} issued.")
        return dict(status="applied", service=a["code"])

    def deliver(self, recipient):
        if recipient=="printer":
            body=dict(schedules=self.services(),editions=self.owners())
        else:
            req=self.request(); result=self.pair(req['minimum'])
            if not result: return dict(status="refused",reason="No issued connection meets the passenger allowance.")
            body=dict(route=result[0],minimum=req['minimum'],editions=self.owners())
        receipt=self.db.execute("INSERT INTO delivered(recipient,body) VALUES(?,?)",(recipient,json.dumps(body))).lastrowid
        event(self.db,f"Receipt {receipt} issued to {recipient}.")
        return dict(status='delivered',receipt=receipt,recipient=recipient,body=body)

def operate(root, name, arg=None, recipient="passenger"):
    path=root/"desk.db"
    if not path.is_file(): raise ValueError("Initialize the desk first.")
    with closing(sqlite3.connect(path)) as db, db:
        db.row_factory=sqlite3.Row
        d=Desk(db)
        if name in ('activity','progress','history','inspect'):
            db.execute('BEGIN')  # Same snapshot across all reads. Opening a view writes nothing.
        else:
            db.execute('BEGIN IMMEDIATE')
        if name in ('activity','progress'): result=getattr(d,name)(recipient)
        elif name=='history': result=d.history(int(arg))
        elif name=='inspect': result=d.inspect(arg)
        elif name=='amend': result=d.amend(arg)
        elif name in ('print','issue'): result=d.deliver('printer' if name=='print' else 'passenger')
        elif name=='change':
            change(db,int(arg)); result=dict(status="changed")
        elif name=='withdraw':
            cancelled=db.execute("UPDATE amendment SET status='withdrawn' WHERE id=? AND status='offered'",(arg,)).rowcount
            if cancelled:
                event(db,f"Hill withdrew {arg}.")
            result=dict(status="withdrawn" if cancelled else "no open offer")
        elif name=='confirm':
            if arg!='N9': raise ValueError('Unknown report.')
            if db.execute("SELECT state FROM report WHERE id='N9'").fetchone()[0]!='confirmed':
                db.execute("INSERT INTO service VALUES('H7','Hill','Mill','Hill',1161,1192)")
                db.execute("UPDATE work SET revision=revision+1 WHERE owner='Hill'")
                db.execute("UPDATE report SET state='confirmed',account='Hill issued H7 at 19:21.' WHERE id='N9'")
                event(db,"Hill issued H7 following N9.")
            result=dict(status="confirmed",service="H7")
        else: raise ValueError("Unknown operation")
        return result,d.reads

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory",type=Path)
    sub=parser.add_subparsers(dest='name',required=True)
    setup=sub.add_parser('init'); setup.add_argument('--noise',type=int,default=40)
    setup.add_argument('--at',choices=['opening','interrupted','retained'],default='interrupted')
    for name in ['activity','progress']:
        sub.add_parser(name).add_argument('--for',choices=['passenger','printer'],dest='recipient',default='passenger')
    sub.add_parser('inspect').add_argument('arg',choices=['request','services','owners','amendments','reports','receipts'])
    sub.add_parser('history').add_argument('arg',type=int)
    for name in ['amend','change','confirm','withdraw']: sub.add_parser(name).add_argument('arg')
    for name in ['issue','print']: sub.add_parser(name)
    args=parser.parse_args()
    if args.name=='init':
        if args.noise<0: parser.error('noise must be nonnegative')
        init(args.directory,args.noise,args.at); print("Desk initialized."); return
    result,_=operate(args.directory,args.name,getattr(args,'arg',None),getattr(args,'recipient','passenger'))
    if args.name in ('activity','progress'): print('\n'.join(result))
    else: print(json.dumps(result,indent=2))

if __name__=="__main__":
    main()
