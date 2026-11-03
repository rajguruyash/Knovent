## Assumptions made, and questions for the owner

Nothing below was invented: it is where the FAQ is silent, and what the engine does about it.

- **Basic vs Pro.** The only difference the FAQ states is how often interior vacuum cleaning is included (Basic once a month, Pro four times a month).
  If the plans also differ in price, number of washes or anything else, add it to `interior-and-plans`, `basic-vacuum` and `pro-vacuum`.
- **Membership prices** are not in the FAQ, so questions such as "what is the price of the Basic plan" or "membership cost per month" are treated as out of scope and refused.
- **What counts as a "bike".** The FAQ only says "bike". Words such as two-wheeler, scooter, motorcycle or bicycle were deliberately not added to any phrasing. Confirm what is covered.
- **SUVs.** Only SUV deep cleaning (₹250) is listed; an SUV body wash is not mentioned, so none is claimed.
- **Bike and car together.** The FAQ gives the two prices but no bundle price or discount; the entry says only that. If a bundle price exists, add it.
- **Not in the FAQ, so refused:** offers and discounts, payment methods, working hours, days of the week, booking and slots, cancellation and refunds, how long a wash takes, water and chemicals, other vehicle types, other cleaning services (polish, coating, engine bay).
- **Other localities.** The FAQ names Kharadi, Pune. A question about another area is answered with the service area and the instruction to contact WASHO to confirm.

### Merges (only where the previous answers carried the same information)

- `bike-wash` = previous "Do you wash bikes?" + "How much does a bike wash cost?" (both: bike washing, ₹65).
- `suv-deep-cleaning` = "Do you clean SUVs?" + "What is the price for SUV deep cleaning?" + "Does WASHO offer SUV services?" (all: SUV deep cleaning, ₹250).
- `body-vs-deep` = "What does car deep cleaning cost compared with a body wash?" + "What is the difference between a car body wash and deep cleaning?" (the same prices and the same distinction).
- `doorstep-area` = "Do you provide doorstep washing?" + "Where does WASHO operate?" + "Do I need to take my vehicle to a washing centre?" + "Can I get my car washed at my apartment?" + "How do I find out whether my address is covered?" (doorstep service in Kharadi, Pune, and contact WASHO to confirm an address).
- `custom-membership` = "Can I get a customized membership?" + "Can I ask about a custom price for multiple washes?" (customized plans; contact the team).

### Kept apart on purpose (they differ), and made easier to tell apart

- Bike only (`bike-wash`) vs bike and car together (`bike-and-car-price`).
- Basic (`basic-vacuum`) vs Pro (`pro-vacuum`), with an overview of both in `interior-and-plans`; each answer now also states the other plan's figure.
- Car body wash price (`car-body-wash-price`) vs car deep cleaning price (`car-deep-cleaning-price`) vs the comparison (`body-vs-deep`).
- Car deep cleaning (`car-deep-cleaning-price`) vs SUV deep cleaning (`suv-deep-cleaning`): each answer states both prices.
- Services overview (`services`, now listing the four services and memberships) vs where WASHO operates (`doorstep-area`).
