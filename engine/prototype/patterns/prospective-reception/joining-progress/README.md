# Returning to the evening desk

A passenger and a printer use the same two timetables. Run the desk with a new local directory, then look at each recipient's current situation.

```sh
python3 -B desk.py /tmp/evening-work init
python3 -B desk.py /tmp/evening-work progress
python3 -B desk.py /tmp/evening-work progress --for printer
```

The passenger’s new transfer requirement can make two finished timetables unusable together. The printer can still receive them. One operator can admit a specific correction to an ended run. A radio report is a lead to check. The example computes actual eligible connections from the issued schedules. For an offered change it calculates whether the potential connection would meet this passenger's requirement. The timetable owner rechecks its basis when admitting a correction. Each recipient receives its own output.

To try the alternative entrance, open `activity` on that same desk and use the same owner reads and actions. Opening either display is passive. If an existing service meets the commission, use the direct issue operation. After using an amendment, inspect the actual itinerary or the delivered timetables. Reopening for another question can change what the desk should show even when its source files remain unchanged.

[Local controls](COMMANDS.md) · [Shift commission](COMMISSION.md)

The example runs on one trusted SQLite host. The operator policy, candidate connections, owner routes and boarding evidence are supplied. A useful interface in another domain needs its own joining judgment and actual recipients. The full working archive includes the original adverse projection, measured acquisition costs and receiving replays.
