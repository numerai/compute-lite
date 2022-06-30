import os
import unittest
import warnings
from ncl.deploy import deploy


class TestDeploy(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        warnings.simplefilter('ignore', ResourceWarning)

    def setUp(self):
        os.environ['AWS_ACCESS_KEY_ID'] = 'AKIA6KIL2OSXKRS5U2G2'
        os.environ['AWS_SECRET_ACCESS_KEY'] = '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'
        os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'

    def test_deploy_model(self):
        model_id = '102052af-a3f4-44ea-b4e4-8d419d3ee4e2'
        model_name = '4chanes'
        deploy('', '', model_id, model_name)


if __name__ == '__main__':
    unittest.main()