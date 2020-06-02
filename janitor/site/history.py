#!/usr/bin/python3

from janitor import state
from janitor.site import env


async def write_history(conn, worker=None, limit=None):
    template = env.get_template('history.html')
    return await template.render_async(
        count=limit,
        history=state.iter_runs(conn, worker=worker, limit=limit))
