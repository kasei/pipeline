import re
import sys
import warnings
import pprint
from contextlib import suppress

from bonobo.config import Option, Service, Configurable, use

from cromulent import model, vocab
from cromulent.extract import extract_physical_dimensions

import pipeline.execution
from pipeline.util import implode_date, timespan_from_outer_bounds, CaseFoldingSet
from pipeline.util.cleaners import \
			parse_location_name, \
			date_cleaner
import pipeline.linkedart
from pipeline.linkedart import add_crom_data, get_crom_object
from pipeline.util import truncate_with_ellipsis

#mark - Auction of Lot - Physical Object

class PopulateSalesObject(Configurable, pipeline.linkedart.PopulateObject):
	helper = Option(required=True)
	post_sale_map = Service('post_sale_map')
	unique_catalogs = Service('unique_catalogs')
	subject_genre = Service('subject_genre')
	destruction_types_map = Service('destruction_types_map')

	def populate_destruction_events(self, data:dict, note, *, type_map, location=None):
		destruction_types_map = type_map
		hmo = get_crom_object(data)
		title = data.get('title')
		short_title = truncate_with_ellipsis(title, 100) or title

		r = re.compile(r'[Dd]estroyed(?: (?:by|during) (\w+))?(?: in (\d{4})[.]?)?')
		m = r.search(note)
		if m:
			method = m.group(1)
			year = m.group(2)
			dest_id = hmo.id + '-Destr'
			d = model.Destruction(ident=dest_id, label=f'Destruction of “{short_title}”')
			d.referred_to_by = vocab.Note(ident='', content=note)
			if year is not None:
				begin, end = date_cleaner(year)
				ts = timespan_from_outer_bounds(begin, end)
				ts.identified_by = model.Name(ident='', content=year)
				d.timespan = ts

			if method:
				with suppress(KeyError, AttributeError):
					type_name = destruction_types_map[method.lower()]
					otype = vocab.instances[type_name]
					event = model.Event(label=f'{method.capitalize()} event causing the destruction of “{short_title}”')
					event.classified_as = otype
					d.caused_by = event
					data['_events'].append(add_crom_data(data={}, what=event))

			if location:
				current = parse_location_name(location, uri_base=self.helper.uid_tag_prefix)
				base_uri = hmo.id + '-Place,'
				place_data = self.helper.make_place(current, base_uri=base_uri)
				place = get_crom_object(place_data)
				if place:
					data['_locations'].append(place_data)
					d.took_place_at = place

			hmo.destroyed_by = d

	def _populate_object_visual_item(self, data:dict, subject_genre):
		hmo = get_crom_object(data)
		title = data.get('title')
		title = truncate_with_ellipsis(title, 100) or title

		vi_id = hmo.id + '-VisItem'
		vi = model.VisualItem(ident=vi_id)
		vidata = {'uri': vi_id}
		if title:
			vidata['label'] = f'Visual work of “{title}”'
			sales_record = get_crom_object(data['_record'])
			vidata['names'] = [(title,{'referred_to_by': [sales_record]})]

		for key in ('genre', 'subject'):
			if key in data:
				values = [v.strip() for v in data[key].split(';')]
				for value in values:
					for prop, mapping in subject_genre.items():
						if value in mapping:
							aat_url = mapping[value]
							type = model.Type(ident=aat_url, label=value)
							setattr(vi, prop, type)
		data['_visual_item'] = add_crom_data(data=vidata, what=vi)
		hmo.shows = vi

	def _populate_object_catalog_record(self, data:dict, parent, lot, cno, rec_num):
		hmo = get_crom_object(data)

		catalog_uri = self.helper.make_proj_uri('CATALOG', cno)
		catalog = vocab.AuctionCatalogText(ident=catalog_uri, label=f'Sale Catalog {cno}')

		record_uri = self.helper.make_proj_uri('CATALOG', cno, 'RECORD', rec_num)
		lot_object_id = parent['lot_object_id']
		
		puid = parent.get('persistent_puid')
		puid_id = self.helper.gri_number_id(puid)

		record = vocab.ParagraphText(ident=record_uri, label=f'Sale recorded in catalog: {lot_object_id} (record number {rec_num})')
		record_data	= {'uri': record_uri}
		record_data['identifiers'] = [model.Name(ident='', content=f'Record of sale {lot_object_id}'), puid_id]
		record.part_of = catalog

		if parent.get('transaction'):
			record.referred_to_by = vocab.PropertyStatusStatement(ident='', label='Transaction type for sales record', content=parent['transaction'])
		record.about = hmo

		data['_record'] = add_crom_data(data=record_data, what=record)
		return record

	def _populate_object_destruction(self, data:dict, parent, destruction_types_map):
		notes = parent.get('auction_of_lot', {}).get('lot_notes')
		if notes and notes.lower().startswith('destroyed'):
			self.populate_destruction_events(data, notes, type_map=destruction_types_map)

	def _populate_object_present_location(self, data:dict, now_key, destruction_types_map):
		hmo = get_crom_object(data)
		location = data.get('present_location')
		if location:
			loc = location.get('geog')
			note = location.get('note')
			if loc:
				if 'destroyed ' in loc.lower():
					self.populate_destruction_events(data, loc, type_map=destruction_types_map)
				elif isinstance(note, str) and 'destroyed ' in note.lower():
					# the object was destroyed, so any "present location" data is actually
					# an indication of the location of destruction.
					self.populate_destruction_events(data, note, type_map=destruction_types_map, location=loc)
				else:
					# TODO: if `parse_location_name` fails, still preserve the location string somehow
					current = parse_location_name(loc, uri_base=self.helper.uid_tag_prefix)
					inst = location.get('inst')
					if inst:
						owner_data = {
							'label': f'{inst} ({loc})',
							'identifiers': [
								model.Name(ident='', content=inst)
							]
						}
						ulan = None
						with suppress(ValueError, TypeError):
							ulan = int(location.get('insi'))
						if ulan:
							owner_data['ulan'] = ulan
							owner_data['uri'] = self.helper.make_proj_uri('ORG', 'ULAN', ulan)
						else:
							owner_data['uri'] = self.helper.make_proj_uri('ORG', 'NAME', inst, 'PLACE', loc)
					else:
						owner_data = {
							'label': '(Anonymous organization)',
							'uri': self.helper.make_proj_uri('ORG', 'CURR-OWN', *now_key),
						}

					if note:
						owner_data['note'] = note

					base_uri = hmo.id + '-Place,'
					place_data = self.helper.make_place(current, base_uri=base_uri)
					place = get_crom_object(place_data)

					make_la_org = pipeline.linkedart.MakeLinkedArtOrganization()
					owner_data = make_la_org(owner_data)
					owner = get_crom_object(owner_data)

					acc = location.get('acc')
					if acc:
						acc_number = vocab.AccessionNumber(ident='', content=acc)
						hmo.identified_by = acc_number
						assignment = model.AttributeAssignment(ident='')
						assignment.carried_out_by = owner
						acc_number.assigned_by = assignment

					owner.residence = place
					data['_locations'].append(place_data)
					data['_final_org'] = owner_data
			else:
				pass # there is no present location place string

	def _populate_object_notes(self, data:dict, parent, unique_catalogs):
		hmo = get_crom_object(data)
		notes = data.get('hand_note', [])
		for note in notes:
			hand_note_content = note['hand_note']
			owner = note.get('hand_note_so')
			cno = parent['auction_of_lot']['catalog_number']
			catalog_uri = self.helper.make_proj_uri('CATALOG', cno, owner, None)
			catalogs = unique_catalogs.get(catalog_uri)
			note = vocab.Note(ident='', content=hand_note_content)
			hmo.referred_to_by = note
			if catalogs and len(catalogs) == 1:
				note.carried_by = vocab.AuctionCatalog(ident=catalog_uri, label=f'Sale Catalog {cno}, owned by “{owner}”')

		inscription = data.get('inscription')
		if inscription:
			hmo.referred_to_by = vocab.InscriptionStatement(ident='', content=inscription)

	def _populate_object_prev_post_sales(self, data:dict, this_key, post_sale_map):
		hmo = get_crom_object(data)
		post_sales = data.get('post_sale', [])
		prev_sales = data.get('prev_sale', [])
		prev_post_sales_records = [(post_sales, False), (prev_sales, True)]
		for sales_data, rev in prev_post_sales_records:
			for sale_record in sales_data:
				pcno = sale_record.get('cat')
				plno = sale_record.get('lot')
# 				plot = self.helper.shared_lot_number_from_lno(plno)
				pdate = implode_date(sale_record, '')
				if pcno and plno and pdate:
					if pcno == 'NA':
						desc = f'Also sold in an unidentified sale: {plno} ({pdate})'
						note = vocab.Note(ident='', content=desc)
						hmo.referred_to_by = note
					else:
						that_key = (pcno, plno, pdate)
						if rev:
							# `that_key` is for a previous sale for this object
							post_sale_map[this_key] = that_key
						else:
							# `that_key` is for a later sale for this object
							post_sale_map[that_key] = this_key

	def __call__(self, data:dict, post_sale_map, unique_catalogs, subject_genre, destruction_types_map):
		'''Add modeling for an object described by a sales record'''
		hmo = get_crom_object(data)
		parent = data['parent_data']
		auction_data = parent.get('auction_of_lot')
		if auction_data:
			lno = str(auction_data['lot_number'])
			data.setdefault('identifiers', [])
			if not lno:
				warnings.warn(f'Setting empty identifier on {hmo.id}')
			data['identifiers'].append(vocab.LotNumber(ident='', content=lno))
		else:
			warnings.warn(f'***** NO AUCTION DATA FOUND IN populate_object')


		cno = auction_data['catalog_number']
		lno = auction_data['lot_number']
		date = implode_date(auction_data, 'lot_sale_')
		lot = self.helper.shared_lot_number_from_lno(lno) # the current key for this object; may be associated later with prev and post object keys
		now_key = (cno, lno, date)

		data['_locations'] = []
		data['_events'] = []
		record = self._populate_object_catalog_record(data, parent, lot, cno, parent['pi_record_no'])
		self._populate_object_visual_item(data, subject_genre)
		self._populate_object_destruction(data, parent, destruction_types_map)
		self.populate_object_statements(data)
		self._populate_object_present_location(data, now_key, destruction_types_map)
		self._populate_object_notes(data, parent, unique_catalogs)
		self._populate_object_prev_post_sales(data, now_key, post_sale_map)
		for p in data.get('portal', []):
			url = p['portal_url']
			hmo.referred_to_by = vocab.WebPage(ident=url, label=url)

		if 'title' in data:
			title = data['title']
			if not hasattr(hmo, '_label'):
				typestring = data.get('object_type', 'Object')
				hmo._label = f'{typestring}: “{title}”'
			del data['title']
			shorter = truncate_with_ellipsis(title, 100)
			if shorter:
				description = vocab.Description(ident='', content=title)
				description.referred_to_by = record
				hmo.referred_to_by = description
				title = shorter
			t = vocab.PrimaryName(ident='', content=title)
			t.classified_as = model.Type(ident='http://vocab.getty.edu/aat/300417193', label='Title')
			t.referred_to_by = record
			data['identifiers'].append(t)

		for d in data.get('other_titles', []):
			title = d['title']
			t = vocab.Name(ident='', content=title)
			data['identifiers'].append(t)

		return data

@use('vocab_type_map')
def add_object_type(data, vocab_type_map):
	'''Add appropriate type information for an object based on its 'object_type' name'''
	typestring = data.get('object_type', '')
	if typestring in vocab_type_map:
		clsname = vocab_type_map.get(typestring, None)
		otype = getattr(vocab, clsname)
		add_crom_data(data=data, what=otype(ident=data['uri']))
	elif ';' in typestring:
		parts = [s.strip() for s in typestring.split(';')]
		if all([s in vocab_type_map for s in parts]):
			types = [getattr(vocab, vocab_type_map[s]) for s in parts]
			obj = vocab.make_multitype_obj(*types, ident=data['uri'])
			add_crom_data(data=data, what=obj)
		else:
			warnings.warn(f'*** Not all object types matched for {typestring!r}')
			add_crom_data(data=data, what=model.HumanMadeObject(ident=data['uri']))
	else:
		warnings.warn(f'*** No object type for {typestring!r}')
		add_crom_data(data=data, what=model.HumanMadeObject(ident=data['uri']))

	parent = data['parent_data']
	coll_data = parent.get('_lot_object_set')
	if coll_data:
		coll = get_crom_object(coll_data)
		if coll:
			data['member_of'] = [coll]

	return data

class AddArtists(Configurable):
	helper = Option(required=True)
	attribution_modifiers = Service('attribution_modifiers')
	attribution_group_types = Service('attribution_group_types')

	def __call__(self, data:dict, *, attribution_modifiers, attribution_group_types):
		'''Add modeling for artists as people involved in the production of an object'''
		hmo = get_crom_object(data)
		data['_organizations'] = []
		data['_original_objects'] = []

		try:
			hmo_label = f'{hmo._label}'
		except AttributeError:
			hmo_label = 'object'
		event_id = hmo.id + '-Prod'
		event = model.Production(ident=event_id, label=f'Production event for {hmo_label}')
		hmo.produced_by = event

		artists = data.get('_artists', [])

		sales_record = get_crom_object(data['_record'])
		pi = self.helper.person_identity

		for a in artists:
			a.setdefault('referred_to_by', [])
			a.update({
				'pi_record_no': data['pi_record_no'],
				'ulan': a['artist_ulan'],
				'auth_name': a['art_authority'],
				'name': a['artist_name']
			})
			if a.get('biography'):
				bio = a['biography']
				del a['biography']
				cite = vocab.BiographyStatement(ident='', content=bio)
				a['referred_to_by'].append(cite)

		def is_or_anon(data:dict):
			if pi.is_anonymous(data):
				mods = {m.lower().strip() for m in data.get('attrib_mod_auth', '').split(';')}
				return 'or' in mods
			return False
		or_anon_records = any([is_or_anon(a) for a in artists])
		uncertain_attribution = or_anon_records

		all_mods = {m.lower().strip() for a in artists for m in a.get('attrib_mod_auth', '').split(';')} - {''}
		artist_group = (not or_anon_records) and (all_mods == {'or'}) # the artist is *one* of the named people, model as a group
		
		other_artists = []
		if artist_group:
			group_id = event.id + '-ArtistGroup'
			g_label = f'Group containing the artist of {hmo_label}'
			g = vocab.UncertainMemberClosedGroup(ident=group_id, label=g_label)
			for seq_no, a in enumerate(artists):
				artist = self.helper.add_person(a, record=sales_record, relative_id=f'artist-{seq_no+1}', role='artist')
				add_crom_data(a, artist)
				artist.member_of = g
			other_artists = artists

			pi_record_no = data['pi_record_no']
			group_uri_key = ('GROUP', 'PI', pi_record_no, 'ArtistGroup')
			group_uri = self.helper.make_proj_uri(*group_uri_key)
			group_data = {
				'uri_keys': group_uri_key,
				'uri': group_uri,
				'role_label': 'uncertain artist'
			}
			artists = [add_crom_data(group_data, g)]
		else:
			for seq_no, a in enumerate(artists):
				artist = self.helper.add_person(a, record=sales_record, relative_id=f'artist-{seq_no+1}', role='artist')
				add_crom_data(a, artist)

		for seq_no, a in enumerate(artists):
			person = get_crom_object(a)
			attribute_assignment_id = event.id + f'-artist-assignment-{seq_no}'
			if is_or_anon(a):
				# do not model the "or anonymous" records; they turn into uncertainty on the other records
				continue
# 			person = self.helper.add_person(a, record=sales_record, relative_id=f'artist-{seq_no+1}', role='artist')
			artist_label = a.get('role_label')

			mod = a.get('attrib_mod_auth', '')
			mods = CaseFoldingSet({m.strip() for m in mod.split(';')} - {''})
			attrib_assignment_classes = [model.AttributeAssignment]
			
			if uncertain_attribution or 'or' in mods:
				attrib_assignment_classes.append(vocab.PossibleAssignment)
				
			if mods:
				# TODO: this should probably be in its own JSON service file:
				STYLE_OF = attribution_modifiers['style of']
				FORMERLY_ATTRIBUTED_TO = attribution_modifiers['formerly attributed to']
				ATTRIBUTED_TO = attribution_modifiers['attributed to']
				COPY_AFTER = attribution_modifiers['copy after']
				PROBABLY = attribution_modifiers['probably by']
				POSSIBLY = attribution_modifiers['possibly by']
				UNCERTAIN = attribution_modifiers['uncertain']

				GROUP_TYPES = set(attribution_group_types.values())
				GROUP_MODS = {k for k, v in attribution_group_types.items() if v in GROUP_TYPES}

				if 'copy by' in mods:
					# equivalent to no modifier
					pass
				elif ATTRIBUTED_TO.intersects(mods):
					# equivalent to no modifier
					pass
				elif STYLE_OF.intersects(mods):
					assignment = vocab.make_multitype_obj(*attrib_assignment_classes, ident=attribute_assignment_id, label=f'In the style of {artist_label}')
					event.attributed_by = assignment
					assignment.assigned_property = 'influenced_by'
					assignment.property_classified_as = vocab.instances['style of']
					assignment.assigned = person
					continue
				elif mods.intersects(GROUP_MODS):
					mod_name = list(GROUP_MODS & mods)[0] # TODO: use all matching types?
					clsname = attribution_group_types[mod_name]
					cls = getattr(vocab, clsname)
					group_label = f'{clsname} of {artist_label}'
					group_id = a['uri'] + f'-{clsname}'
					group = cls(ident=group_id, label=group_label)
					formation = model.Formation(ident='', label=f'Formation of {group_label}')
					formation.influenced_by = person
					group.formed_by = formation
					group_data = add_crom_data({'uri': group_id}, group)
					data['_organizations'].append(group_data)

					subevent_id = event_id + f'-{seq_no}' # TODO: fix for the case of post-sales merging
					subevent = model.Production(ident=subevent_id, label=f'Production sub-event for {group_label}')
					subevent.carried_out_by = group

					if uncertain_attribution:
						assignment = vocab.make_multitype_obj(*attrib_assignment_classes, ident=attribute_assignment_id, label=f'Possibly attributed to {group_label}')
						event.attributed_by = assignment
						assignment.assigned_property = 'part'
						assignment.assigned = subevent
					else:
						event.part = subevent
					continue
				elif FORMERLY_ATTRIBUTED_TO.intersects(mods):
					# the {uncertain_attribution} flag does not apply to this branch, because this branch is not making a statement
					# about a previous attribution. the uncertainty applies only to the current attribution.
					assignment = vocab.ObsoleteAssignment(ident=attribute_assignment_id, label=f'Formerly attributed to {artist_label}')
					event.attributed_by = assignment
					assignment.assigned_property = 'carried_out_by'
					assignment.assigned = person
					continue
				elif UNCERTAIN.intersects(mods):
					if POSSIBLY.intersects(mods):
						attrib_assignment_classes.append(vocab.PossibleAssignment)
						assignment = vocab.make_multitype_obj(*attrib_assignment_classes, ident=attribute_assignment_id, label=f'Possibly attributed to {artist_label}')
						assignment._label = f'Possibly by {artist_label}'
					else:
						attrib_assignment_classes.append(vocab.ProbableAssignment)
						assignment = vocab.make_multitype_obj(*attrib_assignment_classes, ident=attribute_assignment_id, label=f'Probably attributed to {artist_label}')
						assignment._label = f'Probably by {artist_label}'
					event.attributed_by = assignment
					assignment.assigned_property = 'carried_out_by'
					assignment.assigned = person
					continue
				elif COPY_AFTER.intersects(mods):
					# the {uncertain_attribution} flag does not apply to this branch, because this branch is not making a statement
					# about the artist of the work, but about the artist of the original work that this work is a copy of.
					cls = type(hmo)
					original_id = hmo.id + '-Orig'
					original_label = f'Original of {hmo_label}'
					original_hmo = cls(ident=original_id, label=original_label)
					original_event_id = original_hmo.id + '-Prod'
					original_event = model.Production(ident=original_event_id, label=f'Production event for {original_label}')
					original_hmo.produced_by = original_event

					original_subevent_id = original_event_id + f'-{seq_no}' # TODO: fix for the case of post-sales merging
					original_subevent = model.Production(ident=original_subevent_id, label=f'Production sub-event for {artist_label}')
					original_event.part = original_subevent
					original_subevent.carried_out_by = person

					event.influenced_by = original_hmo
					data['_original_objects'].append(add_crom_data(data={}, what=original_hmo))
					continue
				elif mods & {'or', 'and'}:
					pass
				else:
					warnings.warn(f'UNHANDLED attrib_mod_auth VALUE: {mods}')
					pprint.pprint(a, stream=sys.stderr)
					continue

			subprod_path = self.helper.make_uri_path(*a["uri_keys"])
			subevent_id = event_id + f'-{subprod_path}'
			subevent = model.Production(ident=subevent_id, label=f'Production sub-event for {artist_label}')
			subevent.carried_out_by = person
			if uncertain_attribution or 'or' in mods:
				assignment = vocab.make_multitype_obj(*attrib_assignment_classes, ident=attribute_assignment_id, label=f'Possibly attributed to {artist_label}')
				event.attributed_by = assignment
				assignment.assigned_property = 'part'
				assignment.assigned = subevent
			else:
				event.part = subevent
		
		all_artists = [a for a in artists if not is_or_anon(a)] + other_artists
		data['_artists'] = all_artists
		return data
