"""Run one upstream paper bot, with read-only health telemetry for the dashboard."""
import asyncio
import fcntl
import json
import signal
import sys
import time
from pathlib import Path
from paths import RUNTIME, SOURCES, STATE_PATHS

NAME = sys.argv[1]
if NAME not in SOURCES:
    raise SystemExit('Unknown bot')
DATA = RUNTIME/'data'
DATA.mkdir(parents=True, exist_ok=True)
lock = (DATA/(NAME+'.lock')).open('a')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
state_path = STATE_PATHS[NAME]
state_path.parent.mkdir(parents=True, exist_ok=True)
state_lock = state_path.with_name(state_path.name + '.lock').open('a')
fcntl.flock(state_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
sys.path.insert(0, str(SOURCES[NAME]))
if NAME == 'final':
    from binance_cascade_hunter_dynamic_v2_paper import V2PaperHunter as Hunter
elif NAME == 'final3':
    from cascade_final3_paper import Final3PaperHunter as Hunter
else:
    from cascade_final4_paper import Final4PaperHunter as Hunter


def snapshot(bot, status):
    state = json.loads(bot.state_path.read_text())
    sc = bot.scanner
    mono, wall = sc.clock(), sc.wall_clock()
    health = []
    for symbol in sc.symbols:
        received = sc.received.get(symbol)
        event = sc.event_times.get(symbol)
        age = max(0, mono-received) if received is not None else None
        event_age = wall-event/1000 if event is not None else None
        fresh = (sc.connected and age is not None and age <= 10
                 and event_age is not None and -10 <= event_age <= 10)
        health.append({'symbol': symbol, 'age': age, 'event_age': event_age,
                       'fresh': fresh, 'seeded': symbol in sc.histories})
    state.update(bot=NAME, status=status, updated_at=time.time(), scanner={
        'connected': sc.connected, 'symbols': list(sc.symbols), 'events': sc.events,
        'entries': sc.entries, 'book_checks': sc.book_checks,
        'last_event_age': min((x['age'] for x in health if x['age'] is not None), default=None),
        'fresh_symbols': sum(x['fresh'] for x in health), 'symbol_health': health,
    })
    target = DATA/(NAME+'.snapshot.json')
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, allow_nan=False))
    temp.replace(target)


async def main():
    bot = Hunter(state_path=STATE_PATHS[NAME])
    bot.save()
    snapshot(bot, 'starting')
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    async def report():
        while True:
            bot.save()
            snapshot(bot, 'running')
            await asyncio.sleep(3)
    run = asyncio.create_task(bot.start())
    reporter = asyncio.create_task(report())
    stop = asyncio.create_task(stopped.wait())
    status = 'stopped'
    try:
        done, _ = await asyncio.wait((run,reporter,stop),return_when=asyncio.FIRST_COMPLETED)
        for task in (run,reporter):
            if task in done:
                await task
    except Exception:
        status = 'error'
        raise
    finally:
        # Cancel each child once; allow upstream finally blocks to close clients.
        for task in (run,reporter,stop):
            if not task.done(): task.cancel()
        await asyncio.gather(run,reporter,stop,return_exceptions=True)
        bot.save()
        snapshot(bot,status)


if __name__ == '__main__':
    asyncio.run(main())
