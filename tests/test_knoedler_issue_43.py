#!/usr/bin/env python3 -B
import unittest

from cromulent import vocab

from tests import TestKnoedlerPipelineOutput, classified_identifiers

vocab.add_attribute_assignment_check()

class PIRModelingTest_AR43(TestKnoedlerPipelineOutput):
    '''
    AR-43: Fix 'Attributed to' Modifier use
    '''
    def test_modeling_ar43(self):
        output = self.run_pipeline('ar43')

        import pprint
        objects = output['model-object']
        obj1 = objects['tag:getty.edu,2019:digital:pipeline:REPLACE-WITH-UUID:knoedler#Object,7843']
        obj2 = objects['tag:getty.edu,2019:digital:pipeline:REPLACE-WITH-UUID:knoedler#Object,8498']
        obj3 = objects['tag:getty.edu,2019:digital:pipeline:REPLACE-WITH-UUID:knoedler#Object,14490']

        self.verify_production_assignment(obj1, 'JACQUE, CHARLES EMILE', 'JACQUE%2C%20CHARLES%20EMILE')
        self.verify_production_assignment(obj2, 'CUYP, AELBERT', 'CUYP%2C%20AELBERT')
        self.verify_production_assignment(obj3, 'PREDIS, GIOVANNI AMBROGIO DE', 'PREDIS%2C%20GIOVANNI%20AMBROGIO%20DE')

    def verify_production_assignment(self, obj, name, uri_suffix):
        prod = obj['produced_by']
        
        # There are no sub-parts of the production, since all the known
        # information has the 'attributed to' modifier, causing it to be
        # asserted indirectly via an attribution assignment.
        if 'part' in prod:
        	import pprint
        	pprint.pprint(prod)
        self.assertNotIn('part', prod)
        
        # There is an attribute assignment carried out by the 'attributed to' creator
        self.assertIn('attributed_by', prod)
        attr = prod['attributed_by']
        self.assertEqual(len(attr), 1)
        self.assertEqual(attr[0]['_label'], f'Possibly attributed to {name}')
        self.assertEqual(attr[0]['assigned']['carried_out_by'][0]['id'], f'tag:getty.edu,2019:digital:pipeline:REPLACE-WITH-UUID:shared#PERSON,AUTH,{uri_suffix}')

if __name__ == '__main__':
    unittest.main()
