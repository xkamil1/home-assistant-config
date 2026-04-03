# Solar Confidence

Kombinuje Met.no + OWM do solar_confidence 0-100%.

## Feedback loop
1. Predikce -> InfluxDB (30min)
2. Verifikace vs vyroby (hodinu v :05)
3. Kalibrace vah (denne 23:30)
4. FS korekce (denne 23:30)

## InfluxDB
- solar_prediction
- solar_prediction_accuracy
