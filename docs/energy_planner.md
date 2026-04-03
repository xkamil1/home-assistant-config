# Energy Planner

Denne ve 23:05 planuje nabijeni Elroq.

## Strategie
1. SOC <= 20% -> kriticke
2. Slunce do 2 dnu -> cekat
3. Slunce za 3-5 dnu -> nabit mezeru
4. Vikend slunce -> cekat
5. SOC <= 25% -> nabit
6. SOC OK -> nenabijat

## SOC/km
Dynamicky z SOC/range. Typicky 0.20%/km.

## Interakce
input_text -> Haiku -> pending -> confirm/reject
