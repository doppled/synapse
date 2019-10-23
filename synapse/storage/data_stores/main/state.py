# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from collections import namedtuple
from typing import Iterable, Tuple

from six import iteritems

from twisted.internet import defer

from synapse.api.constants import EventTypes
from synapse.api.errors import NotFoundError
from synapse.events import EventBase
from synapse.events.snapshot import EventContext
from synapse.storage._base import SQLBaseStore
from synapse.storage.background_updates import BackgroundUpdateStore
from synapse.storage.data_stores.main.events_worker import EventsWorkerStore
from synapse.storage.state import StateFilter
from synapse.util.caches import intern_string
from synapse.util.caches.descriptors import cached, cachedList
from synapse.util.stringutils import to_ascii

logger = logging.getLogger(__name__)


MAX_STATE_DELTA_HOPS = 100


class _GetStateGroupDelta(
    namedtuple("_GetStateGroupDelta", ("prev_group", "delta_ids"))
):
    """Return type of get_state_group_delta that implements __len__, which lets
    us use the itrable flag when caching
    """

    __slots__ = []

    def __len__(self):
        return len(self.delta_ids) if self.delta_ids else 0


# this inherits from EventsWorkerStore because it calls self.get_events
class StateGroupWorkerStore(EventsWorkerStore, SQLBaseStore):
    """The parts of StateGroupStore that can be called from workers.
    """

    def __init__(self, db_conn, hs):
        super(StateGroupWorkerStore, self).__init__(db_conn, hs)

    @defer.inlineCallbacks
    def get_room_version(self, room_id):
        """Get the room_version of a given room

        Args:
            room_id (str)

        Returns:
            Deferred[str]

        Raises:
            NotFoundError if the room is unknown
        """
        # for now we do this by looking at the create event. We may want to cache this
        # more intelligently in future.

        # Retrieve the room's create event
        create_event = yield self.get_create_event_for_room(room_id)
        return create_event.content.get("room_version", "1")

    @defer.inlineCallbacks
    def get_room_predecessor(self, room_id):
        """Get the predecessor room of an upgraded room if one exists.
        Otherwise return None.

        Args:
            room_id (str)

        Returns:
            Deferred[unicode|None]: predecessor room id

        Raises:
            NotFoundError if the room is unknown
        """
        # Retrieve the room's create event
        create_event = yield self.get_create_event_for_room(room_id)

        # Return predecessor if present
        return create_event.content.get("predecessor", None)

    @defer.inlineCallbacks
    def get_create_event_for_room(self, room_id):
        """Get the create state event for a room.

        Args:
            room_id (str)

        Returns:
            Deferred[EventBase]: The room creation event.

        Raises:
            NotFoundError if the room is unknown
        """
        state_ids = yield self.get_current_state_ids(room_id)
        create_id = state_ids.get((EventTypes.Create, ""))

        # If we can't find the create event, assume we've hit a dead end
        if not create_id:
            raise NotFoundError("Unknown room %s" % (room_id))

        # Retrieve the room's create event and return
        create_event = yield self.get_event(create_id)
        return create_event

    @cached(max_entries=100000, iterable=True)
    def get_current_state_ids(self, room_id):
        """Get the current state event ids for a room based on the
        current_state_events table.

        Args:
            room_id (str)

        Returns:
            deferred: dict of (type, state_key) -> event_id
        """

        def _get_current_state_ids_txn(txn):
            txn.execute(
                """SELECT type, state_key, event_id FROM current_state_events
                WHERE room_id = ?
                """,
                (room_id,),
            )

            return {
                (intern_string(r[0]), intern_string(r[1])): to_ascii(r[2]) for r in txn
            }

        return self.runInteraction("get_current_state_ids", _get_current_state_ids_txn)

    # FIXME: how should this be cached?
    def get_filtered_current_state_ids(self, room_id, state_filter=StateFilter.all()):
        """Get the current state event of a given type for a room based on the
        current_state_events table.  This may not be as up-to-date as the result
        of doing a fresh state resolution as per state_handler.get_current_state

        Args:
            room_id (str)
            state_filter (StateFilter): The state filter used to fetch state
                from the database.

        Returns:
            Deferred[dict[tuple[str, str], str]]: Map from type/state_key to
            event ID.
        """

        where_clause, where_args = state_filter.make_sql_filter_clause()

        if not where_clause:
            # We delegate to the cached version
            return self.get_current_state_ids(room_id)

        def _get_filtered_current_state_ids_txn(txn):
            results = {}
            sql = """
                SELECT type, state_key, event_id FROM current_state_events
                WHERE room_id = ?
            """

            if where_clause:
                sql += " AND (%s)" % (where_clause,)

            args = [room_id]
            args.extend(where_args)
            txn.execute(sql, args)
            for row in txn:
                typ, state_key, event_id = row
                key = (intern_string(typ), intern_string(state_key))
                results[key] = event_id

            return results

        return self.runInteraction(
            "get_filtered_current_state_ids", _get_filtered_current_state_ids_txn
        )

    @defer.inlineCallbacks
    def get_canonical_alias_for_room(self, room_id):
        """Get canonical alias for room, if any

        Args:
            room_id (str)

        Returns:
            Deferred[str|None]: The canonical alias, if any
        """

        state = yield self.get_filtered_current_state_ids(
            room_id, StateFilter.from_types([(EventTypes.CanonicalAlias, "")])
        )

        event_id = state.get((EventTypes.CanonicalAlias, ""))
        if not event_id:
            return

        event = yield self.get_event(event_id, allow_none=True)
        if not event:
            return

        return event.content.get("canonical_alias")

    @cached(max_entries=50000)
    def _get_state_group_for_event(self, event_id):
        return self._simple_select_one_onecol(
            table="event_to_state_groups",
            keyvalues={"event_id": event_id},
            retcol="state_group",
            allow_none=True,
            desc="_get_state_group_for_event",
        )

    @cachedList(
        cached_method_name="_get_state_group_for_event",
        list_name="event_ids",
        num_args=1,
        inlineCallbacks=True,
    )
    def _get_state_group_for_events(self, event_ids):
        """Returns mapping event_id -> state_group
        """
        rows = yield self._simple_select_many_batch(
            table="event_to_state_groups",
            column="event_id",
            iterable=event_ids,
            keyvalues={},
            retcols=("event_id", "state_group"),
            desc="_get_state_group_for_events",
        )

        return {row["event_id"]: row["state_group"] for row in rows}


class StateBackgroundUpdateStore(BackgroundUpdateStore):

    CURRENT_STATE_INDEX_UPDATE_NAME = "current_state_members_idx"
    EVENT_STATE_GROUP_INDEX_UPDATE_NAME = "event_to_state_groups_sg_index"

    def __init__(self, db_conn, hs):
        super(StateBackgroundUpdateStore, self).__init__(db_conn, hs)

        self.register_background_index_update(
            self.CURRENT_STATE_INDEX_UPDATE_NAME,
            index_name="current_state_events_member_index",
            table="current_state_events",
            columns=["state_key"],
            where_clause="type='m.room.member'",
        )
        self.register_background_index_update(
            self.EVENT_STATE_GROUP_INDEX_UPDATE_NAME,
            index_name="event_to_state_groups_sg_index",
            table="event_to_state_groups",
            columns=["state_group"],
        )


class StateStore(StateGroupWorkerStore, StateBackgroundUpdateStore):
    """ Keeps track of the state at a given event.

    This is done by the concept of `state groups`. Every event is a assigned
    a state group (identified by an arbitrary string), which references a
    collection of state events. The current state of an event is then the
    collection of state events referenced by the event's state group.

    Hence, every change in the current state causes a new state group to be
    generated. However, if no change happens (e.g., if we get a message event
    with only one parent it inherits the state group from its parent.)

    There are three tables:
      * `state_groups`: Stores group name, first event with in the group and
        room id.
      * `event_to_state_groups`: Maps events to state groups.
      * `state_groups_state`: Maps state group to state events.
    """

    def __init__(self, db_conn, hs):
        super(StateStore, self).__init__(db_conn, hs)

    def _store_event_state_mappings_txn(
        self, txn, events_and_contexts: Iterable[Tuple[EventBase, EventContext]]
    ):
        state_groups = {}
        for event, context in events_and_contexts:
            if event.internal_metadata.is_outlier():
                continue

            # if the event was rejected, just give it the same state as its
            # predecessor.
            if context.rejected:
                state_groups[event.event_id] = context.prev_group
                continue

            state_groups[event.event_id] = context.state_group

        self._simple_insert_many_txn(
            txn,
            table="event_to_state_groups",
            values=[
                {"state_group": state_group_id, "event_id": event_id}
                for event_id, state_group_id in iteritems(state_groups)
            ],
        )

        for event_id, state_group_id in iteritems(state_groups):
            txn.call_after(
                self._get_state_group_for_event.prefill, (event_id,), state_group_id
            )
