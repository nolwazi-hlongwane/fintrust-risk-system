"""FinTrust Cloud-Based Transaction Insights and Risk Monitoring System.

Package layout (one module per responsibility, as required by the brief):

    config      SSM Parameter Store loading and environment variables
    log         structured JSON logging helper used by every module
    db          psycopg2 connection and rollback pattern        (Week 13 Day 2)
    dynamo      DynamoDB helper functions                       (Week 13 Day 2)
    models      Transaction and RiskScore dataclasses           (Week 13 Day 2)
    validators  input sanitisation and POPIA-aware checks       (Week 13 Day 4)
    ingest      S3-trigger Lambda                               (Week 13 Day 4)
    score       SQS-trigger Lambda                              (Week 13 Day 5)
    query       API Gateway Lambda                              (Week 13 Day 5)
    alert       SES alert Lambda                                (Week 14 Day 1)
    report      EventBridge monthly Lambda                      (Week 14 Day 4)
"""

__version__ = "0.1.0"
