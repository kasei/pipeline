#!/usr/bin/env python3 -B
import unittest
import os
import os.path
import hashlib
import json
import uuid
import pprint
import inspect
from itertools import groupby
from pathlib import Path
import warnings

from tests import TestSalesPipelineOutput
from cromulent import vocab

vocab.add_attribute_assignment_check()

class PIRModelingTest_PrivateContractSales(TestSalesPipelineOutput):
	def test_modeling_private_contract_sales(self):
		'''
		Test for modeling of Private Contract Sales.
		'''
		output = self.run_pipeline('lottery')
		self.verify_catalogs(output)
		self.verify_sales(output)
	
	def verify_catalogs(self, output):
		'''
		For this non-auction sale event, there should be a 'Private Contract Sale' event,
		and all physical copies of the sales catalog should be both classified as an
		'Exhibition Catalog', and carry the same text.
		'''
		objects = output['model-object']
		sale_activities = output['model-sale-activity']
		texts = output['model-lo']
		
		expected_catalog_text_id = 'tag:getty.edu,2019:digital:pipeline:REPLACE-WITH-UUID:sales#CATALOG,D-A50'
		expected_event_id = 'tag:getty.edu,2019:digital:pipeline:REPLACE-WITH-UUID:sales#LOTTERY-EVENT,D-A50'
		
		# there is a single non-auction 'Private Contract Sale' event, and it is referred to by the catalog text
		pvt_sale = sale_activities[expected_event_id]
		self.assertEqual(pvt_sale['_label'], 'Lottery Event D-A50 (1765)')
		self.assertIn(expected_catalog_text_id, {r.get('id') for r in pvt_sale['referred_to_by']})
		
		# there is 1 physical Lottery Catalog
		phys_catalogs = [o for o in objects.values() if o['classified_as'][0]['_label'] == 'Lottery Catalog']
		self.assertEqual(len(phys_catalogs), 1)
		
		# all physical catalogs carry the same catalog text
		catalog_text_ids = set()
		for o in phys_catalogs:
			for text in o['carries']:
				catalog_text_ids.add(text['id'])
		self.assertEqual(catalog_text_ids, {expected_catalog_text_id})
		self.assertIn(expected_catalog_text_id, texts)

		catalog_text = texts[expected_catalog_text_id]
		
		self.assertEqual(len(objects), 4) # 1 physical catalog and 3 objects sold

	def verify_sales(self, output):
		'''
		For a lottery record, there should be:
		
		* A private sale activity classified as a Lottery
		* An Object Set classified as a Collection
		* A HumanMadeObject classified as a Painting, and belonging to the Object Set
		* An Activity modeling the individual private sale
		'''
		objects = output['model-object']
		sale_activities = output['model-sale-activity']
		sets = output['model-set']
		texts = output['model-lo']

		hmo_key = 'tag:getty.edu,2019:digital:pipeline:REPLACE-WITH-UUID:sales#OBJ,D-A50,0001,1765'
		hmo = objects[hmo_key]
		
		sale_curr = sale_activities['tag:getty.edu,2019:digital:pipeline:REPLACE-WITH-UUID:sales#AUCTION,D-A50,0001,1765']
		
		event_key = 'tag:getty.edu,2019:digital:pipeline:REPLACE-WITH-UUID:sales#LOTTERY-EVENT,D-A50'
		sale_event = sale_activities[event_key]
		
		object_set_key = 'tag:getty.edu,2019:digital:pipeline:REPLACE-WITH-UUID:sales#AUCTION,D-A50,0001,1765-Set'
		object_set = sets[object_set_key]
		
		self.assertEqual({c['_label'] for c in sale_event['classified_as']}, {'Lottery'})
		self.assertEqual({c['_label'] for c in object_set['classified_as']}, {'Collection'})

		self.assertIn(object_set_key, {s['id'] for s in hmo['member_of']})
		
		# There are no acquisitions or payments as the transaction is 'unknown'.
		self.assertNotIn('part', sale_curr)


if __name__ == '__main__':
	unittest.main()
