# -*- coding: utf-8 -*-
# Copyright 2019 The Matrix.org Foundation C.I.C.
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

from synapse.storage.data_stores.state import StateGroupDataStore


class DataStores(object):
    """The various data stores.

    These are low level interfaces to physical databases.
    """

    def __init__(self, main_store, db_conn, hs):
        # Note we pass in the main store here as workers use a different main
        # store.
        self.main = main_store
        self.state = StateGroupDataStore(db_conn, hs)
