"""Regression: calibration must use the cohort that actually contains the forecast."""
import unittest
from outcome_ledger import kpis, calibrate


class CalibrationBoundaries(unittest.TestCase):
    def bins(self, second_count=30):
        entries={}
        for probability,correct,n in [(.25,False,30),(.5,True,second_count),(.7,False,30),(.85,True,30)]:
            for i in range(n):
                entries[str(probability)+':'+str(i)]=dict(origin='AI_ESEGUITA',economic_status='VALIDO',
                    net_pnl_usd=1,forecast_probability=probability,direction_correct=correct)
        return kpis(entries)['confidence_calibration']

    def test_boundaries_select_the_correct_observed_bin(self):
        for probability,expected in [(.5,1),(.7,0),(.85,1),(1,1)]:
            with self.subTest(probability=probability):
                result=calibrate(probability,self.bins())
                self.assertEqual(result['calibrated_probability'],expected)
                self.assertEqual(result['original_probability'],probability)

    def test_insufficient_bin_cannot_borrow_neighbour_sample(self):
        result=calibrate(.5,self.bins(second_count=29))
        self.assertIsNone(result['calibrated_probability'])
        self.assertEqual(result['method'],'INSUFFICIENT_SAMPLE')

    def test_forecast_sample_is_independent_of_missing_economic_costs(self):
        result=kpis({'test':dict(origin='AI_ESEGUITA',economic_status='DATI_MANCANTI',net_pnl_usd=None,
            forecast_probability=.7,direction_correct=True)})
        self.assertEqual(result['n'],0);self.assertEqual(result['forecast_n'],1)


if __name__=='__main__':unittest.main()
