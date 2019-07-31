'''
Classes and utility functions for instantiating, configuring, and
running a bonobo pipeline for converting Provenance Index CSV data into JSON-LD.
'''

# PIR Extracters

import re
import os
import json
import sys
import uuid
import csv
import pprint
import pathlib
import itertools
import datetime
import dateutil.parser
from collections import Counter, defaultdict, namedtuple
from contextlib import suppress
import inspect

import timeit
from sqlalchemy import create_engine

import graphviz
import bonobo
from bonobo.config import use, Service, Configurable
from bonobo.nodes import Limit

import settings
from cromulent import model, vocab
from pipeline.projects import PipelineBase
from pipeline.projects.provenance.util import *
from pipeline.util import RecursiveExtractKeyedValue, ExtractKeyedValue, ExtractKeyedValues, \
			MatchingFiles, identity, implode_date, timespan_before, timespan_after, \
			replace_key_pattern, strip_key_prefix
from cromulent.extract import extract_physical_dimensions, extract_monetary_amount
from pipeline.util.cleaners import \
			parse_location, \
			parse_location_name, \
			date_parse, \
			date_cleaner
from pipeline.io.file import MergingFileWriter
# from pipeline.io.arches import ArchesWriter
from pipeline.linkedart import \
			add_crom_data, \
			get_crom_object, \
			MakeLinkedArtHumanMadeObject, \
			MakeLinkedArtAuctionHouseOrganization, \
			MakeLinkedArtOrganization, \
			make_la_person, \
			make_la_place
from pipeline.io.csv import CurriedCSVReader
from pipeline.nodes.basic import \
			AddFieldNames, \
			GroupRepeatingKeys, \
			GroupKeys, \
			AddArchesModel, \
			Serializer, \
			Trace
from pipeline.util.rewriting import rewrite_output_files, JSONValueRewriter

PROBLEMATIC_RECORD_URI = 'tag:getty.edu,2019:digital:pipeline:ProblematicRecord'

#mark - utility functions and classes

def auction_event_for_catalog_number(catalog_number):
	'''
	Return a `vocab.AuctionEvent` object and its associated 'uid' key and URI, based on
	the supplied `catalog_number`.
	'''
	uid = f'AUCTION-EVENT-CATALOGNUMBER-{catalog_number}'
	uri = pir_uri('AUCTION-EVENT', 'CATALOGNUMBER', catalog_number)
	auction = vocab.AuctionEvent(ident=uri)
	auction._label = f"Auction Event for {catalog_number}"
	return auction, uid, uri

def add_auction_event(data):
	'''Add modeling for an auction event based on properties of the supplied `data` dict.'''
	cno = data['catalog_number']
	auction, uid, uri = auction_event_for_catalog_number(cno)
	data['uid'] = uid
	data['uri'] = uri
	add_crom_data(data=data, what=auction)
	yield data

#mark - Places

def auction_event_location(data):
	'''
	Based on location data in the supplied `data` dict, construct a data structure
	representing a hierarchy of places (e.g. location->city->country), and return it.

	This structure will be suitable for passing to `pipeline.linkedart.make_la_place`
	to construct a Place model object.
	'''
	specific_name = data.get('specific_loc')
	city_name = data.get('city_of_sale')
	country_name = data.get('country_auth_1')

	parts = [v for v in (specific_name, city_name, country_name) if v is not None]
	loc = parse_location(*parts, uri_base=UID_TAG_PREFIX, types=('Place', 'City', 'Country'))
	return loc

#mark - Auction Events

@use('auction_locations')
def populate_auction_event(data, auction_locations):
	'''Add modeling data for an auction event'''
	cno = data['catalog_number']
	auction = get_crom_object(data)
	catalog = data['_catalog']['_LOD_OBJECT']

	location_data = data['location']
	current = auction_event_location(location_data)

	# make_la_place is called here instead of as a separate graph node because the Place object
	# gets stored in the `auction_locations` object to be used in the second graph component
	# which uses the data to associate the place with auction lots.
	place_data = make_la_place(current)
	place = get_crom_object(place_data)
	if place:
		data['_locations'] = [place_data]
		auction.took_place_at = place
		auction_locations[cno] = place

	ts = timespan_from_outer_bounds(
		begin=implode_date(data, 'sale_begin_', clamp='begin'),
		end=implode_date(data, 'sale_end_', clamp='end'),
	)

	if ts:
		auction.timespan = ts

	auction.subject_of = catalog
	yield data

def add_auction_house_data(a):
	'''Add modeling data for an auction house organization.'''
	catalog = a.get('_catalog')

	ulan = None
	with suppress(ValueError, TypeError):
		ulan = int(a.get('ulan'))
	if ulan:
		key = f'AUCTION-HOUSE-ULAN-{ulan}'
		a['uid'] = key
		a['uri'] = pir_uri('AUCTION-HOUSE', 'ULAN', ulan)
		a['identifiers'] = [model.Identifier(content=ulan)]
		a['ulan'] = ulan
		house = vocab.AuctionHouseOrg(ident=a['uri'])
	else:
		# not enough information to identify this person uniquely, so they get a UUID
		a['uuid'] = str(uuid.uuid4())
		uri = "urn:uuid:%s" % a['uuid']
		house = vocab.AuctionHouseOrg(ident=uri)

	name = a.get('auc_house_name', a.get('name'))
	a['identifiers'] = []
	if name:
		n = model.Name(content=name)
		n.referred_to_by = catalog
		a['identifiers'].append(n)
		a['label'] = name
	else:
		a['label'] = '(Anonymous)'

	auth = a.get('auc_house_auth')
	if auth:
		n = vocab.PrimaryName()
		n.content = auth
		a['identifiers'].append(n)

	add_crom_data(data=a, what=house)
	return a

@use('auction_houses')
def add_auction_houses(data, auction_houses):
	'''
	Add modeling data for the auction house organization(s) associated with an auction
	event.
	'''
	auction = get_crom_object(data)
	catalog = data['_catalog']['_LOD_OBJECT']
	d = data.copy()
	houses = data.get('auction_house', [])
	cno = data['catalog_number']

	house_objects = []

	for h in houses:
		h['_catalog'] = catalog
		add_auction_house_data(h)
		house = get_crom_object(h)
		auction.carried_out_by = house
		if auction_houses:
			house_objects.append(house)
	auction_houses[cno] = house_objects
	yield d


#mark - Auction of Lot

class AddAuctionOfLot(Configurable):
	'''Add modeling data for the auction of a lot of objects.'''
	
	problematic_records = Service('problematic_records')
	auction_locations = Service('auction_locations')
	auction_houses = Service('auction_houses')
	def __init__(self, *args, **kwargs):
		self.lot_cache = {}
		super().__init__(*args, **kwargs)

	@staticmethod
	def shared_lot_number_from_lno(lno):
		'''
		Given a `lot_number` value which identifies an object in a group, strip out the
		object-specific content, returning an identifier for the entire lot.

		For example, strip the object identifier suffixes such as '[a]':

		'0001[a]' -> '0001'
		'''
		# TODO: does this handle all the cases of data packed into the lot_number string that need to be stripped?
		r = re.compile(r'(\[[a-z]\])')
		m = r.search(lno)
		if m:
			return lno.replace(m.group(1), '')
		return lno

	@staticmethod
	def set_lot_auction_houses(lot, cno, auction_houses):
		'''Associate the auction house with the auction lot.'''
		houses = auction_houses.get(cno)
		if houses:
			for house in houses:
				lot.carried_out_by = house

	@staticmethod
	def set_lot_location(lot, cno, auction_locations):
		'''Associate the location with the auction lot.'''
		place = auction_locations.get(cno)
		if place:
			lot.took_place_at = place

	@staticmethod
	def set_lot_date(lot, auction_data):
		'''Associate a timespan with the auction lot.'''
		date = implode_date(auction_data, 'lot_sale_')
		dates = date_parse(date, delim='-')
		if dates:
			bounds = map(lambda v: v.strftime("%Y-%m-%dT%H:%M:%SZ"), dates)
		else:
			bounds = []
		if bounds:
			ts = timespan_from_outer_bounds(*bounds)
			ts.identified_by = model.Name(ident='', content=date)
			lot.timespan = ts

	@staticmethod
	def shared_lot_number_ids(cno, lno, date):
		shared_lot_number = AddAuctionOfLot.shared_lot_number_from_lno(lno)
		uid = f'AUCTION-{cno}-LOT-{shared_lot_number}-DATE-{date}'
		uri = pir_uri('AUCTION', cno, 'LOT', shared_lot_number, 'DATE', date)
		return uid, uri

	@staticmethod
	def transaction_uri_for_lot(data, prices):
		cno, lno, date = object_key(data)
		shared_lot_number = AddAuctionOfLot.shared_lot_number_from_lno(lno)
		for p in prices:
			n = p.get('price_note')
			if n and n.startswith('for lots '):
				return pir_uri('AUCTION-TX-MULTI', cno, date, n[9:])
		return pir_uri('AUCTION-TX', cno, date, shared_lot_number)

	@staticmethod
	def set_lot_notes(lot, auction_data):
		'''Associate notes with the auction lot.'''
		cno, lno, _ = object_key(auction_data)
		auction, _, _ = auction_event_for_catalog_number(cno)
		notes = auction_data.get('lot_notes')
		if notes:
			lot.referred_to_by = vocab.Note(content=notes)
		lot.identified_by = model.Identifier(content=lno)
		lot.part_of = auction

	def set_lot_objects(self, lot, lno, data):
		'''Associate the set of objects with the auction lot.'''
		coll = vocab.AuctionLotSet(ident=data['uri'])
		shared_lot_number = self.shared_lot_number_from_lno(lno)
		coll._label = f'Auction Lot {shared_lot_number}'
		est_price = data.get('estimated_price')
		if est_price:
			coll.dimension = get_crom_object(est_price)
		start_price = data.get('start_price')
		if start_price:
			coll.dimension = get_crom_object(start_price)

		lot.used_specific_object = coll
		data['_lot_object_set'] = coll

	def __call__(self, data, auction_houses, auction_locations, problematic_records):
		'''Add modeling data for the auction of a lot of objects.'''
		auction_data = data['auction_of_lot']
		lot_object_key = object_key(auction_data)
		cno, lno, date = lot_object_key
		shared_lot_number = self.shared_lot_number_from_lno(lno)
		uid, uri = self.shared_lot_number_ids(cno, lno, date)
		data['uid'] = uid
		data['uri'] = uri

		lot = vocab.Auction(ident=data['uri'])
		lot._label = f'Auction of Lot {cno} {shared_lot_number} ({date})'

		for pcno, plno, pdate, problem in problematic_records.get('lots', []):
			# TODO: this is inefficient, but will probably be OK so long as the number
			#       of problematic records is small. We do it this way because we can't
			#       represent a tuple directly as a JSON dict key, and we don't want to
			#       have to do post-processing on the services JSON files after loading.
			problem_key = (pcno, plno, pdate)
			if problem_key == lot_object_key:
				note = model.LinguisticObject(content=problem)
				note.classified_as = vocab.instances["brief text"]
				note.classified_as = model.Type(
					ident=PROBLEMATIC_RECORD_URI,
					label='Problematic Record'
				)
				lot.referred_to_by = note

		self.set_lot_auction_houses(lot, cno, auction_houses)
		self.set_lot_location(lot, cno, auction_locations)
		self.set_lot_date(lot, auction_data)
		self.set_lot_notes(lot, auction_data)
		self.set_lot_objects(lot, lno, data)
		
		tx_uri = AddAuctionOfLot.transaction_uri_for_lot(auction_data, data.get('price', []))
		tx = vocab.Procurement(ident=tx_uri)
		lot.caused = tx
		tx_data = {}
		with suppress(AttributeError):
			tx_data['_date'] = lot.timespan
		data['_procurement_data'] = add_crom_data(data=tx_data, what=tx)

		add_crom_data(data=data, what=lot)
		yield data

def add_crom_price(data, _):
	'''
	Add modeling data for `MonetaryAmount`, `StartingPrice`, or `EstimatedPrice`,
	based on properties of the supplied `data` dict.
	'''
	amnt = extract_monetary_amount(data)
	if amnt:
		add_crom_data(data=data, what=amnt)
	return data

def add_person(data: dict):
	'''
	Add modeling data for people, based on properties of the supplied `data` dict.

	This function adds properties to `data` before calling
	`pipeline.linkedart.make_la_person` to construct the model objects.
	'''
	ulan = None
	with suppress(ValueError, TypeError):
		ulan = int(data.get('ulan'))
	if ulan:
		key = f'PERSON-ULAN-{ulan}'
		data['uid'] = key
		data['uri'] = pir_uri('PERSON', 'ULAN', ulan)
		data['identifiers'] = [model.Identifier(content=ulan)]
		data['ulan'] = ulan
	else:
		# not enough information to identify this person uniquely, so they get a UUID
		data['uuid'] = str(uuid.uuid4())

	names = []
	for k in ('auth_name', 'name'):
		if k in data:
			names.append((data[k],))
	if names:
		data['names'] = names
		data['label'] = names[0][0]
	else:
		data['label'] = '(Anonymous person)'

	make_la_person(data)
	return data

def final_owner_procurement(final_owner, current_tx, hmo, current_ts):
	tx = related_procurement(current_tx, hmo, current_ts, buyer=final_owner)
	try:
		object_label = hmo._label
		tx._label = f'Procurement leading to the currently known location of “{object_label}”'
	except AttributeError:
		tx._label = f'Procurement leading to the currently known location of object'
	return tx

def add_acquisition(data, hmo, buyers, sellers):
	'''Add modeling of an acquisition as a transfer of title from the seller to the buyer'''
	parent = data['parent_data']
	transaction = parent['transaction']
	prices = parent['price']
	auction_data = parent['auction_of_lot']
	cno, lno, date = object_key(auction_data)
	data['buyer'] = buyers
	data['seller'] = sellers
	object_label = hmo._label
	amnts = [get_crom_object(p) for p in prices]

# 	if not prices:
# 		print(f'*** No price data found for {transaction} transaction')

	acq = model.Acquisition(label=f'Acquisition of {cno} {lno} ({date}): “{object_label}”')
	acq.transferred_title_of = hmo
	paym = model.Payment(label=f'Payment for “{object_label}”')
	for seller in [get_crom_object(s) for s in sellers]:
		paym.paid_to = seller
		acq.transferred_title_from = seller
	for buyer in [get_crom_object(b) for b in buyers]:
		paym.paid_from = buyer
		acq.transferred_title_to = buyer
	for amnt in amnts:
		paym.paid_amount = amnt

	tx_data = parent['_procurement_data']
	current_tx = get_crom_object(tx_data)
	ts = tx_data.get('_date')
	if ts:
		acq.timespan = ts
	current_tx.part = paym
	current_tx.part = acq
	if '_procurements' not in data:
		data['_procurements'] = []
	data['_procurements'] += [add_crom_data(data={}, what=current_tx)]
# 	lot_uid, lot_uri = AddAuctionOfLot.shared_lot_number_ids(cno, lno)
	# TODO: `annotation` here is from add_physical_catalog_objects
# 	paym.referred_to_by = annotation
	add_crom_data(data=data, what=acq)

	final_owner_data = data.get('_final_org')
	if final_owner_data:
		final_owner = get_crom_object(final_owner_data)
		tx = final_owner_procurement(final_owner, current_tx, hmo, ts)
		data['_procurements'].append(add_crom_data(data={}, what=tx))

	post_own = data.get('post_owner', [])
	prev_own = data.get('prev_owner', [])
	prev_post_owner_records = [(post_own, False), (prev_own, True)]
	for owner_data, rev in prev_post_owner_records:
		for owner_record in owner_data:
			name = owner_record.get('own_auth', owner_record.get('own'))
			owner_record['names'] = [(name,)]
			owner_record['label'] = name
			owner_record['uuid'] = str(uuid.uuid4())
			# TODO: handle other fields of owner_record: own_auth_D, own_auth_L, own_auth_Q, own_ques, own_so, own_ulan
			make_la_person(owner_record)
			owner = get_crom_object(owner_record)
			own_info_source = owner_record.get('own_so')
			if own_info_source:
				note = vocab.Note(content=own_info_source)
				hmo.referred_to_by = note
				owner.referred_to_by = note
			tx = related_procurement(current_tx, hmo, ts, buyer=owner, previous=rev)
			ptx_data = tx_data.copy()
			data['_procurements'].append(add_crom_data(data=ptx_data, what=tx))
	yield data

def related_procurement(current_tx, hmo, current_ts=None, buyer=None, seller=None, previous=False):
	'''
	Returns a new `vocab.Procurement` object (and related acquisition) that is temporally
	related to the supplied procurement and associated data. The new procurement is for
	the given object, and has the given buyer and seller (both optional).
	
	If the `previous` flag is `True`, the new procurement is occurs before `current_tx`,
	and if the timespan `current_ts` is given, has temporal data to that effect. If
	`previous` is `False`, this relationship is reversed.
	'''
	tx = vocab.Procurement()
	if current_tx:
		if previous:
			tx.ends_before_the_start_of = current_tx
		else:
			tx.starts_after_the_end_of = current_tx
	modifier_label = 'Previous' if previous else 'Subsequent'
	try:
		pacq = model.Acquisition(label=f'{modifier_label} Acquisition of: “{hmo._label}”')
	except AttributeError:
		pacq = model.Acquisition(label=f'{modifier_label} Acquisition')
	pacq.transferred_title_of = hmo
	if buyer:
		pacq.transferred_title_to = buyer
	if seller:
		pacq.transferred_title_from = seller
	tx.part = pacq
	tx_data = {}
	if current_ts:
		if previous:
			pacq.timespan = timespan_before(current_ts)
		else:
			pacq.timespan = timespan_after(current_ts)
	return tx

def add_bidding(data, buyers):
	'''Add modeling of bids that did not lead to an acquisition'''
	parent = data['parent_data']
	prices = parent['price']
	auction_data = parent['auction_of_lot']
	cno, lno, date = object_key(auction_data)
	amnts = [get_crom_object(p) for p in prices]

	if amnts:
		lot = get_crom_object(parent)
		all_bids = model.Activity(label=f'Bidding on {cno} {lno} ({date})')
		all_bids.part_of = lot

		for amnt in amnts:
			bid = vocab.Bidding()
			try:
				amnt_label = amnt._label
				bid._label = f'Bid of {amnt_label} on {cno} {lno} ({date})'
				prop = model.PropositionalObject(label=f'Promise to pay {amnt_label}')
			except AttributeError:
				bid._label = f'Bid on {cno} {lno} ({date})'
				prop = model.PropositionalObject(label=f'Promise to pay')

			prop.refers_to = amnt
			bid.created = prop

			# TODO: there are often no buyers listed for non-sold records.
			#       should we construct an anonymous person to carry out the bid?
			for buyer in [get_crom_object(b) for b in buyers]:
				bid.carried_out_by = buyer

			all_bids.part = bid

		final_owner_data = data.get('_final_org')
		if final_owner_data:
			final_owner = get_crom_object(final_owner_data)
			ts = lot.timespan
			hmo = get_crom_object(data)
			tx = final_owner_procurement(final_owner, None, hmo, ts)
			if '_procurements' not in data:
				data['_procurements'] = []
			data['_procurements'].append(add_crom_data(data={}, what=tx))

		add_crom_data(data=data, what=all_bids)
		yield data
	else:
		pass
# 			print(f'*** No price data found for {parent['transaction']!r} transaction')

def add_acquisition_or_bidding(data):
	'''Determine if this record has an acquisition or bidding, and add appropriate modeling'''
	parent = data['parent_data']
	transaction = parent['transaction']
	transaction = transaction.replace('[?]', '')
	transaction = transaction.rstrip()

	data = data.copy()
	hmo = get_crom_object(data)

	# TODO: filtering empty people should be moved much earlier in the pipeline
	buyers = [add_person(p) for p in filter_empty_people(*parent['buyer'])]
	sellers = [add_person(p) for p in filter_empty_people(*parent['seller'])]

	# TODO: is this the right set of transaction types to represent acquisition?
	if transaction in ('Sold', 'Vendu', 'Verkauft', 'Bought In'):
		yield from add_acquisition(data, hmo, buyers, sellers)
	elif transaction in ('Unknown', 'Unbekannt', 'Inconnue', 'Withdrawn', 'Non Vendu', ''):
		yield from add_bidding(data, buyers)
	else:
		print(f'Cannot create acquisition data for unknown transaction type: {transaction!r}')

#mark - Single Object Lot Tracking

class TrackLotSizes(Configurable):
	lot_counter = Service('lot_counter')

	def __call__(self, data, lot_counter):
		auction_data = data['auction_of_lot']
		cno, lno, date = object_key(auction_data)
		lot = AddAuctionOfLot.shared_lot_number_from_lno(lno)
		key = (cno, lot, date)
		lot_counter[key] += 1

#mark - Auction of Lot - Physical Object

def genre_instance(value, vocab_instance_map):
	'''Return the appropriate type instance for the supplied genre name'''
	if value is None:
		return None
	value = value.lower()

	vocab.register_instance('animal', {'parent': model.Type, 'id': '300249395', 'label': 'Animal'})
	vocab.register_instance('history', {'parent': model.Type, 'id': '300033898', 'label': 'History'})

	instance_name = vocab_instance_map.get(value)
	if instance_name:
		instance = vocab.instances.get(instance_name)
		if instance:
			print(f'GENRE: {value}')
		else:
			print(f'*** No genre instance available for {instance_name!r} in vocab_instance_map')
		return instance
	return None

def populate_destruction_events(data, note, destruction_types_map):
	hmo = get_crom_object(data)
	title = data.get('title')

	vocab.register_instance('fire', {'parent': model.Type, 'id': '300068986', 'label': 'Fire'})

	r = re.compile(r'Destroyed(?: (?:by|during) (\w+))?(?: in (\d{4})[.]?)?')
	m = r.search(note)
	if m:
		method = m.group(1)
		year = m.group(2)
		d = model.Destruction(label=f'Destruction of “{title}”')
		d.referred_to_by = vocab.Note(content=note)
		if year is not None:
			begin, end = date_cleaner(year)
			ts = timespan_from_outer_bounds(begin, end)
			d.timespan = ts
		hmo.destroyed_by = d

		if method:
			with suppress(KeyError, AttributeError):
				type_name = destruction_types_map[method.lower()]
				type = vocab.instances[type_name]
				event = model.Event(label=f'{method.capitalize()} event causing the destruction of “{title}”')
				event.classified_as = type
				d.caused_by = event
	
@use('post_sale_map')
@use('unique_catalogs')
@use('vocab_instance_map')
@use('destruction_types_map')
def populate_object(data, post_sale_map, unique_catalogs, vocab_instance_map, destruction_types_map):
	'''Add modeling for an object described by a sales record'''
	hmo = get_crom_object(data)
	parent = data['parent_data']
	auction_data = parent.get('auction_of_lot')
	if auction_data:
		lno = auction_data['lot_number']
		if 'identifiers' not in data:
			data['identifiers'] = []
		data['identifiers'].append(model.Identifier(content=lno))
	m = data.get('materials')
	if m:
		matstmt = vocab.MaterialStatement()
		matstmt.content = m
		hmo.referred_to_by = matstmt

	cno = auction_data['catalog_number']
	lno = auction_data['lot_number']
	date = implode_date(auction_data, 'lot_sale_')
	lot = AddAuctionOfLot.shared_lot_number_from_lno(lno)
	now_key = (cno, lot, date) # the current key for this object; may be associated later with prev and post object keys

	title = data.get('title')
	vi = model.VisualItem()
	if title:
		vi._label = f'Visual work of “{title}”'
	genre = genre_instance(data.get('genre'), vocab_instance_map)
	if genre:
		vi.classified_as = genre
	hmo.shows = vi

	location = data.get('present_location')
	if location:
		loc = location.get('geog')
		if loc:
			if 'Destroyed ' in loc:
				populate_destruction_events(data, loc, destruction_types_map)
			else:
				current = parse_location_name(loc, uri_base=UID_TAG_PREFIX)
				place_data = make_la_place(current)
				place = get_crom_object(place_data)
				# TODO: if `parse_location_name` fails, still preserve the location string somehow
				inst = location.get('inst')
				if inst:
					owner_data = {
						'name': inst,
						'label': f'{inst} ({loc})',
					}
					ulan = location.get('insi')
					if ulan:
						owner_data['ulan'] = ulan
						owner_data['uri'] = pir_uri('ORGANIZATION', 'ULAN', ulan)
					else:
						owner_data['uri'] = pir_uri('ORGANIZATION', 'NAME', inst, 'PLACE', loc)
				else:
					owner_data = {
						'label': '(Anonymous organization)',
						'name': '(Anonymous organization)',
						'uri': pir_uri('ORGANIZATION', 'PRESENT-OWNER', *now_key)
					}
				lao = MakeLinkedArtOrganization()
				owner_data = lao(owner_data)
				owner = get_crom_object(owner_data)
				owner.residence = place
				data['_locations'] = [place_data]
				data['_final_org'] = owner_data
		else:
			pass # there is no present location place string
		note = location.get('note')
		if note:
			pass
			# TODO: the acquisition_note needs to be attached as a Note to the final post owner acquisition

	notes = parent.get('auction_of_lot', {}).get('lot_notes')
	if notes and notes.startswith('Destroyed'):
		populate_destruction_events(data, notes, destruction_types_map)

	notes = data.get('hand_note', [])
	for note in notes:
		c = note['hand_note']
		owner = note.get('hand_note_so')
		cno = parent['auction_of_lot']['catalog_number']
		catalog_uri = pir_uri('CATALOG', cno, owner, None)
		catalogs = unique_catalogs.get(catalog_uri)
		note = vocab.Note(content=c)
		hmo.referred_to_by = note
		if catalogs and len(catalogs) == 1:
			note.carried_by = model.HumanMadeObject(ident=catalog_uri, label=f'Sale Catalog {cno}, owned by {owner}')

	inscription = data.get('inscription')
	if inscription:
		hmo.carries = vocab.Note(content=inscription)

	post_sales = data.get('post_sale', [])
	prev_sales = data.get('prev_sale', [])
	prev_post_sales_records = [(post_sales, False), (prev_sales, True)]
	for sales_data, rev in prev_post_sales_records:
		for sale_record in sales_data:
			pcno = sale_record.get('cat')
			plno = sale_record.get('lot')
			plot = AddAuctionOfLot.shared_lot_number_from_lno(plno)
			pdate = implode_date(sale_record, '')
			if pcno and plot and pdate:
				later_key = (pcno, plot, pdate)
				if rev:
					later_key, now_key = now_key, later_key
				post_sale_map[later_key] = now_key

	dimstr = data.get('dimensions')
	if dimstr:
		dimstmt = vocab.DimensionStatement()
		dimstmt.content = dimstr
		hmo.referred_to_by = dimstmt
		for dim in extract_physical_dimensions(dimstr):
			hmo.dimension = dim
		else:
			pass
# 			print(f'No dimension data was parsed from the dimension statement: {dimstr}')
	return data

@use('vocab_type_map')
def add_object_type(data, vocab_type_map):
	'''Add appropriate type information for an object based on its 'object_type' name'''
	typestring = data.get('object_type', '')
	if typestring in vocab_type_map:
		clsname = vocab_type_map.get(typestring, None)
		otype = getattr(vocab, clsname)
		add_crom_data(data=data, what=otype(ident=data['uri']))
	else:
		print(f'*** No object type for {typestring!r}')
		add_crom_data(data=data, what=model.HumanMadeObject(ident=data['uri']))

	parent = data['parent_data']
	coll = parent.get('_lot_object_set')
	if coll:
		data['member_of'] = [coll]

	return data

def add_pir_artists(data):
	'''Add modeling for artists as people involved in the production of an object'''
	lod_object = get_crom_object(data)
	event = model.Production()
	lod_object.produced_by = event

	artists = data.get('_artists', [])

	# TODO: filtering empty people should be moved much earlier in the pipeline
	artists = list(filter_empty_people(*artists))
	data['_artists'] = artists
	for a in artists:
		star_rec_no = a.get('star_rec_no')
		ulan = a.get('artist_ulan')
		if ulan:
			key = f'PERSON-ULAN-{ulan}'
			a['uri'] = pir_uri('PERSON', 'ULAN', ulan)
			a['ulan'] = ulan
		else:
			key = f'PERSON-STAR-{star_rec_no}'
			a['uri'] = pir_uri('PERSON', 'star', star_rec_no)
		a['uid'] = key
		if a.get('artist_name'):
			name = a.get('artist_name')
			a['names'] = [(name,)]
			a['label'] = name
		else:
			a['label'] = '(Anonymous artist)'

		make_la_person(a)
		person = get_crom_object(a)
		subevent = model.Production()
		event.part = subevent
		names = a.get('names')
		if names:
			name = names[0][0]
			subevent._label = f'Production sub-event for {name}'
		subevent.carried_out_by = person
	yield data

#mark - Physical Catalogs

def add_auction_catalog(data):
	'''Add modeling for auction catalogs as linguistic objects'''
	cno = data['catalog_number']
	key = f'CATALOG-{cno}'
	cdata = {'uid': key, 'uri': pir_uri('CATALOG', cno)}
	catalog = vocab.AuctionCatalog(ident=cdata['uri'])
	catalog._label = f'Sale Catalog {cno}'
	data['_catalog'] = cdata

	add_crom_data(data=cdata, what=catalog)
	yield data

def add_physical_catalog_objects(data):
	'''Add modeling for physical copies of an auction catalog'''
	catalog = data['_catalog']['_LOD_OBJECT']
	cno = data['catalog_number']
	owner = data['owner_code']
	copy = data['copy_number']
	uri = pir_uri('CATALOG', cno, owner, copy)
	data['uri'] = uri
	labels = [f'Sale Catalog {cno}', f'owned by {owner}']
	if copy:
		labels.append(f'copy {copy}')
	catalogObject = model.HumanMadeObject(ident=uri, label=', '.join(labels))
	info = data.get('annotation_info')
	if info:
		catalogObject.referred_to_by = vocab.Note(content=info)
	catalogObject.carries = catalog

	# TODO: Rob's build-sample-auction-data.py script adds this annotation. where does it come from?
# 	anno = vocab.Annotation()
# 	anno._label = "Additional annotations in WSHC copy of BR-A1"
# 	catalogObject.carries = anno
	add_crom_data(data=data, what=catalogObject)
	return data

@use('location_codes')
@use('unique_catalogs')
def add_physical_catalog_owners(data, location_codes, unique_catalogs):
	'''Add information about the ownership of a physical copy of an auction catalog'''
	# TODO: Add information about the current owner of the physical catalog copy
	# TODO: are the values of data['owner_code'] mapped somewhere?
	
	# Add the URI of this physical catalog to `unique_catalogs`. This data will be used
	# later to figure out which catalogs can be uniquely identified by a catalog number
	# and owner code (e.g. for owners who do not have multiple copies of a catalog).
	cno = data['catalog_number']
	owner_code = data['owner_code']
	owner_name = None
	try:
		owner_name = location_codes[owner_code]
		data['_owner'] = {
			'name': owner_name,
			'label': owner_name,
			'uri': pir_uri('ORGANIZATION', 'LOCATION-CODE', owner_code),
			'identifiers': [model.Identifier(ident='', content=owner_code)],
		}
		owner = model.Group(ident=data['_owner']['uri'])
		owner_data = add_crom_data(data=data['_owner'], what=owner)
		catalog = get_crom_object(data)
		catalog.current_owner = owner
	except KeyError:
		pass

	uri = pir_uri('CATALOG', cno, owner_code, None)
	if uri not in unique_catalogs:
		unique_catalogs[uri] = set()
	unique_catalogs[uri].add(uri)
	return data


#mark - Physical Catalogs - Informational Catalogs

def populate_auction_catalog(data):
	'''Add modeling data for an auction catalog'''
	d = {k: v for k, v in data.items()}
	parent = data['parent_data']
	cno = parent['catalog_number']
	sno = parent['star_record_no']
	catalog = get_crom_object(d)
	for lno in parent.get('lugt', {}).values():
		catalog.identified_by = model.Identifier(label=f"Lugt Number: {lno}", content=lno)
	catalog.identified_by = model.Identifier(content=cno)
	catalog.identified_by = vocab.LocalNumber(content=sno)
	notes = data.get('notes')
	if notes:
		note = vocab.Note(content=parent['notes'])
		catalog.referred_to_by = note
	yield d

#mark - Provenance Pipeline class

class ProvenancePipeline(PipelineBase):
	'''Bonobo-based pipeline for transforming Provenance data from CSV into JSON-LD.'''
	def __init__(self, input_path, catalogs, auction_events, contents, **kwargs):
		self.project_name = 'provenance'
		self.output_chain = None
		self.graph_0 = None
		self.graph_1 = None
		self.graph_2 = None
		self.models = kwargs.get('models', settings.arches_models)
		self.catalogs_header_file = catalogs['header_file']
		self.catalogs_files_pattern = catalogs['files_pattern']
		self.auction_events_header_file = auction_events['header_file']
		self.auction_events_files_pattern = auction_events['files_pattern']
		self.contents_header_file = contents['header_file']
		self.contents_files_pattern = contents['files_pattern']
		self.limit = kwargs.get('limit')
		self.debug = kwargs.get('debug', False)
		self.input_path = input_path

		fs = bonobo.open_fs(input_path)
		with fs.open(self.catalogs_header_file, newline='') as csvfile:
			r = csv.reader(csvfile)
			self.catalogs_headers = next(r)
		with fs.open(self.auction_events_header_file, newline='') as csvfile:
			r = csv.reader(csvfile)
			self.auction_events_headers = next(r)
		with fs.open(self.contents_header_file, newline='') as csvfile:
			r = csv.reader(csvfile)
			self.contents_headers = next(r)

		if self.debug:
			self.serializer	= Serializer(compact=False)
			self.writer		= None
			# self.writer	= ArchesWriter()
			sys.stderr.write("In DEBUGGING mode\n")
		else:
			self.serializer	= Serializer(compact=True)
			self.writer		= None
			# self.writer	= ArchesWriter()

	# Set up environment
	def get_services(self):
		'''Return a `dict` of named services available to the bonobo pipeline.'''
		services = super().get_services()
		services.update({
			'lot_counter': Counter(),
			'unique_catalogs': {},
			'post_sale_map': {},
			'auction_houses': {},
			'auction_locations': {},
		})
		return services

	def add_serialization_chain(self, graph, input_node):
		'''Add serialization of the passed transformer node to the bonobo graph.'''
		if self.writer is not None:
			graph.add_chain(
				self.serializer,
				self.writer,
				_input=input_node
			)
		else:
			sys.stderr.write('*** No serialization chain defined\n')

	def add_physical_catalog_owners_chain(self, graph, catalogs, serialize=True):
		'''Add modeling of physical copies of auction catalogs.'''
		groups = graph.add_chain(
			ExtractKeyedValue(key='_owner'),
			MakeLinkedArtOrganization(),
			AddArchesModel(model=self.models['Group']),
			_input=catalogs.output
		)
		if serialize:
			# write SALES data
			self.add_serialization_chain(graph, groups.output)
		return groups

	def add_physical_catalogs_chain(self, graph, records, serialize=True):
		'''Add modeling of physical copies of auction catalogs.'''
		catalogs = graph.add_chain(
			AddFieldNames(field_names=self.catalogs_headers),
			add_auction_catalog,
			add_physical_catalog_objects,
			add_physical_catalog_owners,
			AddArchesModel(model=self.models['HumanMadeObject']),
			_input=records.output
		)
		if serialize:
			# write SALES data
			self.add_serialization_chain(graph, catalogs.output)
		return catalogs

	def add_catalog_linguistic_objects(self, graph, events, serialize=True):
		'''Add modeling of auction catalogs as linguistic objects.'''
		los = graph.add_chain(
			ExtractKeyedValue(key='_catalog'),
			populate_auction_catalog,
			AddArchesModel(model=self.models['LinguisticObject']),
			_input=events.output
		)
		if serialize:
			# write SALES data
			self.add_serialization_chain(graph, los.output)
		return los

	def add_auction_events_chain(self, graph, records, serialize=True):
		'''Add modeling of auction events.'''
		auction_events = graph.add_chain(
			AddFieldNames(field_names=self.auction_events_headers),
			GroupRepeatingKeys(mapping={
				'seller': {'prefixes': ('sell_auth_name', 'sell_auth_q')},
				'expert': {'prefixes': ('expert', 'expert_auth', 'expert_ulan')},
				'commissaire': {'prefixes': ('comm_pr', 'comm_pr_auth', 'comm_pr_ulan')},
				'auction_house': {'prefixes': ('auc_house_name', 'auc_house_auth', 'auc_house_ulan')},
			}),
			GroupKeys(mapping={
				'lugt': {'properties': ('lugt_number_1', 'lugt_number_2', 'lugt_number_3')},
				'auc_copy': {
					'properties': (
						'auc_copy_seller_1',
						'auc_copy_seller_2',
						'auc_copy_seller_3',
						'auc_copy_seller_4')},
				'other_seller': {
					'properties': (
						'other_seller_1',
						'other_seller_2',
						'other_seller_3')},
				'title_pg_sell': {'properties': ('title_pg_sell_1', 'title_pg_sell_2')},
				'location': {
					'properties': (
						'city_of_sale',
						'sale_location',
						'country_auth_1',
						'country_auth_2',
						'specific_loc')},
			}),
			add_auction_catalog,
			add_auction_event,
			add_auction_houses,
			populate_auction_event,
			AddArchesModel(model=self.models['Event']),
			_input=records.output
		)
		if serialize:
			# write SALES data
			self.add_serialization_chain(graph, auction_events.output)
		return auction_events

	def add_procurement_chain(self, graph, acquisitions, serialize=True):
		'''Add modeling of the procurement event of an auction of a lot.'''
		p = graph.add_chain(
			ExtractKeyedValues(key='_procurements'),
			AddArchesModel(model=self.models['Procurement']),
			_input=acquisitions.output
		)
		if serialize:
			# write SALES data
			self.add_serialization_chain(graph, p.output)
	
	def add_buyers_sellers_chain(self, graph, acquisitions, serialize=True):
		'''Add modeling of the buyers, bidders, and sellers involved in an auction.'''
		for role in ('buyer', 'seller'):
			p = graph.add_chain(
				ExtractKeyedValues(key=role),
				AddArchesModel(model=self.models['Person']),
				_input=acquisitions.output
			)
			if serialize:
				# write SALES data
				self.add_serialization_chain(graph, p.output)

	def add_acquisitions_chain(self, graph, sales, serialize=True):
		'''Add modeling of the acquisitions and bidding on lots being auctioned.'''
		acqs = graph.add_chain(
			add_acquisition_or_bidding,
			_input=sales.output
		)
		_acqs1 = graph.add_chain(
			AddArchesModel(model=self.models['Activity']),
			_input=acqs.output
		)
		orgs = graph.add_chain(
			ExtractKeyedValue(key='_final_org'),
			AddArchesModel(model=self.models['Group']),
			_input=acqs.output
		)
		
		if serialize:
			# write SALES data
			self.add_serialization_chain(graph, _acqs1.output)
			self.add_serialization_chain(graph, orgs.output)
		return acqs

	def add_sales_chain(self, graph, records, serialize=True):
		'''Add transformation of sales records to the bonobo pipeline.'''
		sales = graph.add_chain(
			AddFieldNames(field_names=self.contents_headers),
			GroupRepeatingKeys(mapping={
				'expert': {'prefixes': ('expert_auth', 'expert_ulan')},
				'commissaire': {'prefixes': ('commissaire_pr', 'comm_ulan')},
				'auction_house': {'prefixes': ('auction_house', 'house_ulan')},
				'_artists': {
					'postprocess': add_pir_record_ids,
					'prefixes': (
						'artist_name',
						'artist_info',
						'art_authority',
						'nationality',
						'attrib_mod',
						'attrib_mod_auth',
						'star_rec_no',
						'artist_ulan')},
				'hand_note': {'prefixes': ('hand_note', 'hand_note_so')},
				'seller': {
					'postprocess': lambda x, _: strip_key_prefix('sell_', x),
					'prefixes': (
						'sell_name',
						'sell_name_so',
						'sell_name_ques',
						'sell_mod',
						'sell_auth_name',
						'sell_auth_nameq',
						'sell_auth_mod',
						'sell_auth_mod_a',
						'sell_ulan')},
				'price': {
					'postprocess': add_crom_price,
					'prefixes': (
						'price_amount',
						'price_currency',
						'price_note',
						'price_source',
						'price_citation')},
				'buyer': {
					'postprocess': lambda x, _: strip_key_prefix('buy_', x),
					'prefixes': (
						'buy_name',
						'buy_name_so',
						'buy_name_ques',
						'buy_name_cite',
						'buy_mod',
						'buy_auth_name',
						'buy_auth_nameQ',
						'buy_auth_mod',
						'buy_auth_mod_a',
						'buy_ulan')},
				'prev_owner': {
					'postprocess': [
						lambda x, _: replace_key_pattern(r'(prev_owner)', 'prev_own', x),
						lambda x, _: strip_key_prefix('prev_', x),
					],
					'prefixes': (
						'prev_owner',
						'prev_own_ques',
						'prev_own_so',
						'prev_own_auth',
						'prev_own_auth_D',
						'prev_own_auth_L',
						'prev_own_auth_Q',
						'prev_own_ulan')},
				'prev_sale': {
					'postprocess': lambda x, _: strip_key_prefix('prev_sale_', x),
					'prefixes': (
						'prev_sale_year',
						'prev_sale_mo',
						'prev_sale_day',
						'prev_sale_loc',
						'prev_sale_lot',
						'prev_sale_ques',
						'prev_sale_artx',
						'prev_sale_ttlx',
						'prev_sale_note',
						'prev_sale_coll',
						'prev_sale_cat')},
				'post_sale': {
					'postprocess': lambda x, _: strip_key_prefix('post_sale_', x),
					'prefixes': (
						'post_sale_year',
						'post_sale_mo',
						'post_sale_day',
						'post_sale_loc',
						'post_sale_lot',
						'post_sale_q',
						'post_sale_art',
						'post_sale_ttl',
						'post_sale_nte',
						'post_sale_col',
						'post_sale_cat')},
				'post_owner': {
					'postprocess': lambda x, _: strip_key_prefix('post_', x),
					'prefixes': (
						'post_own',
						'post_own_q',
						'post_own_so',
						'post_own_auth',
						'post_own_auth_D',
						'post_own_auth_L',
						'post_own_auth_Q',
						'post_own_ulan')},
			}),
			GroupKeys(mapping={
				'present_location': {
					'postprocess': lambda x, _: strip_key_prefix('present_loc_', x),
					'properties': (
						'present_loc_geog',
						'present_loc_inst',
						'present_loc_insq',
						'present_loc_insi',
						'present_loc_acc',
						'present_loc_accq',
						'present_loc_note',
					)
				}
			}),
			GroupKeys(mapping={
				'auction_of_lot': {
					'properties': (
						'catalog_number',
						'lot_number',
						'lot_sale_year',
						'lot_sale_month',
						'lot_sale_day',
						'lot_sale_mod',
						'lot_notes')},
				'_object': {
					'postprocess': add_pir_object_uri,
					'properties': (
						'title',
						'title_modifier',
						'object_type',
						'materials',
						'dimensions',
						'formatted_dimens',
						'format',
						'genre',
						'subject',
						'inscription',
						'present_location',
						'_artists',
						'hand_note',
						'post_sale',
						'prev_sale',
						'prev_owner',
						'post_owner')},
				'estimated_price': {
					'postprocess': add_crom_price,
					'properties': (
						'est_price',
						'est_price_curr',
						'est_price_desc',
						'est_price_so')},
				'start_price': {
					'postprocess': add_crom_price,
					'properties': (
						'start_price',
						'start_price_curr',
						'start_price_desc',
						'start_price_so')},
				'ask_price': {
					'postprocess': add_crom_price,
					'properties': (
						'ask_price',
						'ask_price_curr',
						'ask_price_so')},
			}),
			AddAuctionOfLot(),
			AddArchesModel(model=self.models['Activity']),
			_input=records.output
		)
		if serialize:
			# write SALES data
			self.add_serialization_chain(graph, sales.output)
		return sales

	def add_single_object_lot_tracking_chain(self, graph, sales):
		small_lots = graph.add_chain(
			TrackLotSizes(),
			_input=sales.output
		)
		return small_lots

	def add_object_chain(self, graph, sales, serialize=True):
		'''Add modeling of the objects described by sales records.'''
		objects = graph.add_chain(
			ExtractKeyedValue(key='_object'),
			add_object_type,
			populate_object,
			MakeLinkedArtHumanMadeObject(),
			AddArchesModel(model=self.models['HumanMadeObject']),
			add_pir_artists,
			_input=sales.output
		)
		
		if serialize:
			# write OBJECTS data
			self.add_serialization_chain(graph, objects.output)

		return objects

	def add_places_chain(self, graph, auction_events, serialize=True):
		'''Add extraction and serialization of locations.'''
		places = graph.add_chain(
			ExtractKeyedValues(key='_locations'),
			RecursiveExtractKeyedValue(key='part_of'),
			AddArchesModel(model=self.models['Place']),
			_input=auction_events.output
		)
		if serialize:
			# write OBJECTS data
			self.add_serialization_chain(graph, places.output)
		return places

	def add_auction_houses_chain(self, graph, auction_events, serialize=True):
		'''Add modeling of the auction houses related to an auction event.'''
		houses = graph.add_chain(
			ExtractKeyedValues(key='auction_house'),
			MakeLinkedArtAuctionHouseOrganization(),
			AddArchesModel(model=self.models['Group']),
			_input=auction_events.output
		)
		if serialize:
			# write OBJECTS data
			self.add_serialization_chain(graph, houses.output)
		return houses

	def add_people_chain(self, graph, objects, serialize=True):
		'''Add transformation of artists records to the bonobo pipeline.'''
		model_id = self.models.get('Person', 'XXX-Person-Model')
		people = graph.add_chain(
			ExtractKeyedValues(key='_artists'),
			AddArchesModel(model=model_id),
			_input=objects.output
		)
		if serialize:
			# write PEOPLE data
			self.add_serialization_chain(graph, people.output)
		return people

	def _construct_graph(self, single_graph=False):
		'''
		Construct bonobo.Graph object(s) for the entire pipeline.

		If `single_graph` is `False`, generate two `Graph`s (`self.graph_1` and
		`self.graph_2`), that will be run sequentially. the first for catalogs and events,
		the second for sales auctions (which depends on output from the first).

		If `single_graph` is `True`, then generate a single `Graph` that has the entire
		pipeline in it (`self.graph_0`). This is used to be able to produce graphviz
		output of the pipeline for visual inspection.
		'''
		graph0 = bonobo.Graph()
		graph1 = bonobo.Graph()
		graph2 = bonobo.Graph()

		component1 = [graph0] if single_graph else [graph1]
		component2 = [graph0] if single_graph else [graph2]
		for g in component1:
			physical_catalog_records = g.add_chain(
				MatchingFiles(path='/', pattern=self.catalogs_files_pattern, fs='fs.data.provenance'),
				CurriedCSVReader(fs='fs.data.provenance', limit=self.limit),
			)

			auction_events_records = g.add_chain(
				MatchingFiles(path='/', pattern=self.auction_events_files_pattern, fs='fs.data.provenance'),
				CurriedCSVReader(fs='fs.data.provenance', limit=self.limit),
			)

			catalogs = self.add_physical_catalogs_chain(g, physical_catalog_records, serialize=True)
			_ = self.add_physical_catalog_owners_chain(g, catalogs, serialize=True)
			auction_events = self.add_auction_events_chain(g, auction_events_records, serialize=True)
			_ = self.add_catalog_linguistic_objects(g, auction_events, serialize=True)
			_ = self.add_auction_houses_chain(g, auction_events, serialize=True)
			_ = self.add_places_chain(g, auction_events, serialize=True)

		if not single_graph:
			self.output_chain = None

		for g in component2:
			contents_records = g.add_chain(
				MatchingFiles(path='/', pattern=self.contents_files_pattern, fs='fs.data.provenance'),
				CurriedCSVReader(fs='fs.data.provenance', limit=self.limit)
			)
			sales = self.add_sales_chain(g, contents_records, serialize=True)
			_ = self.add_single_object_lot_tracking_chain(g, sales)
			objects = self.add_object_chain(g, sales, serialize=True)
			_ = self.add_places_chain(g, objects, serialize=True)
			acquisitions = self.add_acquisitions_chain(g, objects, serialize=True)
			self.add_buyers_sellers_chain(g, acquisitions, serialize=True)
			self.add_procurement_chain(g, acquisitions, serialize=True)
			_ = self.add_people_chain(g, objects, serialize=True)

		if single_graph:
			self.graph_0 = graph0
		else:
			self.graph_1 = graph1
			self.graph_2 = graph2

	def get_graph(self):
		'''Return a single bonobo.Graph object for the entire pipeline.'''
		if not self.graph_0:
			self._construct_graph(single_graph=True)

		return self.graph_0

	def get_graph_1(self):
		'''Construct the bonobo pipeline to fully transform Provenance data from CSV to JSON-LD.'''
		if not self.graph_1:
			self._construct_graph()
		return self.graph_1

	def get_graph_2(self):
		'''Construct the bonobo pipeline to fully transform Provenance data from CSV to JSON-LD.'''
		if not self.graph_2:
			self._construct_graph()
		return self.graph_2

	def run(self, services=None, **options):
		'''Run the Provenance bonobo pipeline.'''
		sys.stderr.write("- Limiting to %d records per file\n" % (self.limit,))
		sys.stderr.write("- Using serializer: %r\n" % (self.serializer,))
		sys.stderr.write("- Using writer: %r\n" % (self.writer,))
		if not services:
			services = self.get_services(**options)

		start = timeit.default_timer()
		print('Running graph component 1...')
		graph1 = self.get_graph_1(**options)
		bonobo.run(graph1, services=services)

		print('Running graph component 2...')
		graph2 = self.get_graph_2(**options)
		bonobo.run(graph2, services=services)
		
		print('Pipeline runtime: ', timeit.default_timer() - start)  


class ProvenanceFilePipeline(ProvenancePipeline):
	'''
	Provenance pipeline with serialization to files based on Arches model and resource UUID.

	If in `debug` mode, JSON serialization will use pretty-printing. Otherwise,
	serialization will be compact.
	'''
	def __init__(self, input_path, catalogs, auction_events, contents, **kwargs):
		super().__init__(input_path, catalogs, auction_events, contents, **kwargs)
		self.use_single_serializer = False
		self.output_chain = None
		debug = kwargs.get('debug', False)
		output_path = kwargs.get('output_path')

		if debug:
			self.serializer	= Serializer(compact=False)
			self.writer		= MergingFileWriter(directory=output_path, partition_directories=True)
			# self.writer	= ArchesWriter()
		else:
			self.serializer	= Serializer(compact=True)
			self.writer		= MergingFileWriter(directory=output_path, partition_directories=True)
			# self.writer	= ArchesWriter()

	def add_serialization_chain(self, graph, input_node):
		'''Add serialization of the passed transformer node to the bonobo graph.'''
		if self.use_single_serializer:
			if self.output_chain is None:
				self.output_chain = graph.add_chain(self.serializer, self.writer, _input=None)

			graph.add_chain(identity, _input=input_node, _output=self.output_chain.input)
		else:
			super().add_serialization_chain(graph, input_node)

	def merge_post_sale_objects(self, counter, post_map):
		singles = {k for k in counter if counter[k] == 1}
		multiples = {k for k in counter if counter[k] > 1}
		
		total = 0
		mapped = 0

		rewrite_map_filename = os.path.join(settings.pipeline_tmp_path, 'post_sale_rewrite_map.json')
		sales_tree_filename = os.path.join(settings.pipeline_tmp_path, 'sales-tree.data')

		if os.path.exists(sales_tree_filename):
			with open(sales_tree_filename) as f:
				g = SalesTree.load(f)
		else:
			g = SalesTree()

		for src, dst in post_map.items():
			total += 1
			if dst in singles:
				mapped += 1
				g.add_edge(src, dst)
			elif dst in multiples:
				pass
				print(f'  {src} maps to a MULTI-OBJECT lot')
			else:
				print(f'  {src} maps to an UNKNOWN lot')
		print(f'mapped {mapped}/{total} objects to a previous sale')

		large_components = set(g.largest_component_canonical_keys(10))
		dot = graphviz.Digraph()
		
		node_id = lambda n: f'n{n!s}'
		for n, i in g.nodes.items():
			key, _ = g.canonical_key(n)
			if key in large_components:
				dot.node(node_id(i), str(n))
		
		post_sale_rewrite_map = {}
		if os.path.exists(rewrite_map_filename):
			with open(rewrite_map_filename, 'r') as f:
				with suppress(json.decoder.JSONDecodeError):
					post_sale_rewrite_map = json.load(f)
		print('Rewrite output files, replacing the following URIs:')
		for src, dst in g:
			canonical, steps = g.canonical_key(src)
			src_uri = pir_uri('OBJECT', *src)
			dst_uri = pir_uri('OBJECT', *canonical)
			print(f's/ {src_uri:<100} / {dst_uri:<100} /')
			post_sale_rewrite_map[src_uri] = dst_uri
			if canonical in large_components:
				i = node_id(g.nodes[src])
				j = node_id(g.nodes[dst])
				dot.edge(i, j, f'{steps} steps')

		dot_filename = os.path.join(settings.pipeline_tmp_path, 'sales.dot')
		dot.save(filename=dot_filename)
		with open(rewrite_map_filename, 'w') as f:
			json.dump(post_sale_rewrite_map, f)
			print(f'Saved post-sales rewrite map to {rewrite_map_filename}')
		with open(sales_tree_filename, 'w') as f:
			g.dump(f)

# 		r = JSONValueRewriter(post_sale_rewrite_map)
# 		rewrite_output_files(r)

	def run(self, **options):
		'''Run the Provenance bonobo pipeline.'''
		start = timeit.default_timer()
		services = self.get_services(**options)
		super().run(services=services, **options)

		print('====================================================')
		print('Running post-processing of post-sale data...')
		counter = services['lot_counter']
		post_map = services['post_sale_map']
		self.merge_post_sale_objects(counter, post_map)
		print(f'>>> {len(post_map)} post sales records')
		print('Total runtime: ', timeit.default_timer() - start)  