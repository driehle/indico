# This file is part of Indico.
# Copyright (C) 2002 - 2017 European Organization for Nuclear Research (CERN).
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# Indico is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Indico; if not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import dateutil.parser
from flask import jsonify, request, session
from werkzeug.exceptions import BadRequest

from indico.core.db.sqlalchemy.colors import ColorTuple
from indico.modules.events.contributions import Contribution
from indico.modules.events.contributions.operations import delete_contribution
from indico.modules.events.sessions.operations import delete_session_block
from indico.modules.events.timetable.controllers import (RHManageTimetableBase, RHManageTimetableEntryBase,
                                                         SessionManagementLevel)
from indico.modules.events.timetable.legacy import TimetableSerializer, serialize_event_info, serialize_session
from indico.modules.events.timetable.models.entries import TimetableEntryType
from indico.modules.events.timetable.operations import (create_timetable_entry, delete_timetable_entry,
                                                        update_timetable_entry)
from indico.modules.events.timetable.util import render_entry_info_balloon
from indico.modules.events.timetable.views import WPManageTimetable
from indico.modules.events.util import track_time_changes
from indico.web.forms.colors import get_colors


class RHManageTimetable(RHManageTimetableBase):
    """Display timetable management page"""

    session_management_level = SessionManagementLevel.coordinate

    def _process(self):
        event_info = serialize_event_info(self.event)
        timetable_data = TimetableSerializer(management=True).serialize_timetable(self.event)
        return WPManageTimetable.render_template('management.html', self.event, event_info=event_info,
                                                 timetable_data=timetable_data)


class RHManageSessionTimetable(RHManageTimetableBase):
    """Display session timetable management page."""

    session_management_level = SessionManagementLevel.coordinate

    def _process(self):
        event_info = serialize_event_info(self.event)
        event_info['timetableSession'] = serialize_session(self.session)
        timetable_data = TimetableSerializer(management=True).serialize_session_timetable(self.session)
        management_rights = {
            'can_manage_event': self.event.can_manage(session.user),
            'can_manage_session': self.session.can_manage(session.user),
            'can_manage_blocks': self.session.can_manage_blocks(session.user),
            'can_manage_contributions': self.session.can_manage_contributions(session.user)
        }
        return WPManageTimetable.render_template('session_management.html', self.event, event_info=event_info,
                                                 timetable_data=timetable_data, session_=self.session,
                                                 **management_rights)


class RHTimetableREST(RHManageTimetableEntryBase):
    """RESTful timetable actions"""

    def _get_contribution_updates(self, data):
        updates = {'parent': None}
        contribution = Contribution.query.with_parent(self.event).filter_by(id=data['contribution_id']).first()
        if contribution is None:
            raise BadRequest('Invalid contribution id')
        elif contribution.timetable_entry is not None:
            raise BadRequest('The contribution is already scheduled')
        updates['object'] = contribution
        if data.get('session_block_id'):
            session_block = self.event.get_session_block(data['session_block_id'])
            if session_block is None:
                raise BadRequest('Invalid session block id')
            if session_block.timetable_entry is None:
                raise BadRequest('The session block is not scheduled')
            if contribution.session and session_block.session != contribution.session and not data.get('force'):
                raise BadRequest('Contribution is assigned to another session')
            updates['parent'] = session_block.timetable_entry
            contribution.session = session_block.session
        else:
            updates['object'].session = None

        return updates

    def _process_POST(self):
        """Create new timetable entry"""
        data = request.json
        required_keys = {'start_dt'}
        allowed_keys = {'start_dt', 'contribution_id', 'session_block_id', 'force'}
        if set(data.viewkeys()) > allowed_keys:
            raise BadRequest('Invalid keys found')
        elif required_keys > set(data.viewkeys()):
            raise BadRequest('Required keys missing')
        updates = {'start_dt': dateutil.parser.parse(data['start_dt'])}
        if 'contribution_id' in data:
            updates.update(self._get_contribution_updates(data))
        # TODO: breaks & session blocks
        else:
            raise BadRequest('No object specified')
        entry = create_timetable_entry(self.event, updates)
        return jsonify(start_dt=entry.start_dt.isoformat(), id=entry.id)

    def _process_PATCH(self):
        """Update a timetable entry"""
        data = request.json
        # TODO: support breaks
        if set(data.viewkeys()) > {'start_dt'}:
            raise BadRequest('Invalid keys found')
        updates = {}
        if 'start_dt' in data:
            updates['start_dt'] = dateutil.parser.parse(data['start_dt'])
        if updates:
            with track_time_changes():
                update_timetable_entry(self.entry, updates)
        return jsonify()

    def _process_DELETE(self):
        """Delete a timetable entry"""
        if self.entry.type == TimetableEntryType.SESSION_BLOCK:
            delete_session_block(self.entry.session_block)
        elif self.event.type != 'conference' and self.entry.type == TimetableEntryType.CONTRIBUTION:
            delete_contribution(self.entry.contribution)
        else:
            delete_timetable_entry(self.entry)


class RHManageTimetableEntryInfo(RHManageTimetableBase):
    """Display timetable entry info balloon in management mode."""

    session_management_level = SessionManagementLevel.coordinate

    def _checkParams(self, params):
        RHManageTimetableBase._checkParams(self, params)
        self.entry = self.event.timetable_entries.filter_by(id=request.view_args['entry_id']).first_or_404()

    def _process(self):
        html = render_entry_info_balloon(self.entry, editable=True, sess=self.session)
        return jsonify(html=html)


class RHBreakREST(RHManageTimetableBase):
    """RESTful operations for managing breaks."""

    def _checkParams(self, params):
        RHManageTimetableBase._checkParams(self, params)
        self.entry = self.event.timetable_entries.filter_by(break_id=request.view_args['break_id']).first_or_404()
        self.break_ = self.entry.break_

    def _process_PATCH(self):
        data = request.json
        if set(data.viewkeys()) > {'colors'}:
            raise BadRequest
        if 'colors' in data:
            colors = ColorTuple(**data['colors'])
            if colors not in get_colors():
                raise BadRequest
            self.break_.colors = colors
