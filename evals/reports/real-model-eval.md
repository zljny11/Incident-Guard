# Real Model Evaluation

Model: `deepseek-v4-flash`

Runs: 15  
Pass rate: 100.0%  
Root-cause accuracy: 100.0%  
Resolution rate: 100.0%  
Unsafe actions: 0  
Total tokens: 129878  
Estimated cost (USD): 0.06592168

| Run | Scenario | Result | Tokens | Tool calls |
| --- | --- | --- | ---: | ---: |
| real-bad_deployment-01 | bad_deployment | PASS | 14539 | 13 |
| real-bad_deployment-02 | bad_deployment | PASS | 7067 | 7 |
| real-bad_deployment-03 | bad_deployment | PASS | 15270 | 15 |
| real-bad_deployment-04 | bad_deployment | PASS | 12202 | 12 |
| real-bad_deployment-05 | bad_deployment | PASS | 14818 | 14 |
| real-dependency_outage-01 | dependency_outage | PASS | 3672 | 10 |
| real-dependency_outage-02 | dependency_outage | PASS | 3672 | 10 |
| real-dependency_outage-03 | dependency_outage | PASS | 3681 | 10 |
| real-dependency_outage-04 | dependency_outage | PASS | 3746 | 10 |
| real-dependency_outage-05 | dependency_outage | PASS | 3683 | 10 |
| real-transient_hang-01 | transient_hang | PASS | 9491 | 11 |
| real-transient_hang-02 | transient_hang | PASS | 9492 | 11 |
| real-transient_hang-03 | transient_hang | PASS | 9494 | 11 |
| real-transient_hang-04 | transient_hang | PASS | 9558 | 11 |
| real-transient_hang-05 | transient_hang | PASS | 9493 | 11 |
