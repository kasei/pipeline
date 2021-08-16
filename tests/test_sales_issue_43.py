#!/usr/bin/env python3 -B
import unittest

from cromulent import vocab

from tests import TestSalesPipelineOutput, classified_identifiers

vocab.add_attribute_assignment_check()

class PIRModelingTest_AR43(TestSalesPipelineOutput):
    '''
    AR-43: Fix 'Attributed to' Modifier use
    '''
    def test_modeling_ar43(self):
        output = self.run_pipeline('ar43')
        objects = output['model-object']

        obj1 = objects['tag:getty.edu,2019:digital:pipeline:REPLACE-WITH-UUID:sales#OBJ,B-A138,0022,1774-05-30']
        prod = obj1['produced_by']
        self.assertIn('attributed_by', prod)
        attr = prod['attributed_by']
        self.assertEqual(len(attr), 1)
        self.assertEqual(attr[0]['_label'], 'Possibly attributed to SAVERY (XAVERY)')
        self.assertEqual(attr[0]['assigned']['carried_out_by'][0]['id'], 'tag:getty.edu,2019:digital:pipeline:REPLACE-WITH-UUID:shared#PERSON,AUTH,SAVERY%20%28XAVERY%29')

		# There are no sub-parts of the production, since all the known
		# information has the 'attributed to' modifier, causing it to be
		# asserted indirectly via an attribution assignment.
        self.assertNotIn('part', prod)
        

if __name__ == '__main__':
    unittest.main()