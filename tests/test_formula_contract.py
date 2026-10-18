import hashlib
import unittest
from unittest.mock import patch
from formula_contract import audit,native_formulas


class FormulaAuditTests(unittest.TestCase):
    def sheet(self,formula='=A1'):
        return {'sheets':[{'properties':{'title':'Metriche'},'data':[{'startRow':5,'startColumn':1,'rowData':[{'values':[{'userEnteredValue':{'formulaValue':formula}}]}]}]}]}

    def test_native_coordinates_and_hash(self):
        self.assertEqual(native_formulas(self.sheet()),{'Metriche!B6':hashlib.sha256(b'=A1').hexdigest()})

    def test_changed_formula_and_missing_mandate_anchor_are_reported(self):
        with patch('formula_contract.validate_mandate'),patch('formula_contract.load_policy',return_value={'formula_hashes':native_formulas(self.sheet())}):
            r=audit('Versione: CONS-2026-10-18-r8',self.sheet('=A2'))
        self.assertEqual(r['status'],'CONFLICT')
        self.assertIn('REGISTRY_FORMULA_MISMATCH:Metriche!B6',r['errors'])
        self.assertTrue(any(x.startswith('MANDATE_ANCHOR_MISSING') for x in r['errors']))


if __name__=='__main__':unittest.main()
