# Evidence-aware answers: what went wrong (before the evidence milestone)

Diagnostic set: 121 unique held-out questions from the Pawfect pack (90 answerable, 31 that should be declined). This set has now been read question by question, so it is a development set, not a clean test.

## Where every question went

- answered correctly: 35
- correct abstention: 30
- fabricated answer: 1
- missed answer: 48
- answered from the wrong source: 6
- stored FAQ answer for a document question: 1

The "unaccounted for" questions are the ones that were answered but wrongly: 6 answered from a source that was not expected and 1 given a stored FAQ answer where only the handbook holds the information. They are neither correct nor missed, which is why correct + missed = 83 of 90.

## Failures by class

- paraphrase mismatch: 41
- retrieval failure: 4
- missing evidence: 4
- response generation failure: 3
- partial evidence: 3

## Every failure

### 1. What can I get done for my dog there?
- class: **retrieval failure** · outcome: faq · retrieval similarity 0.33 · sufficiency 1.0 · type question
- expected: faq-001 (What services does Pawfect Grooming Studio offer?: Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific)
- retrieved FAQ: [('faq-008', 0.33), ('faq-002', 0.32), ('faq-001', 0.32)] · passages: [('pawfect-handbook-002', 0.51), ('pawfect-handbook-001', 0.47), ('pawfect-handbook-003', 0.36)]
- check of "What can I get done for my dog there" against faq-001: words covered 1.0, meaning-assisted 1.0, similarity of best sentence 0.42, asks for nothing specific, detail present: True; best sentence: "Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific haircuts."

### 2. Do you offer nail clipping as part of grooming?
- class: **response generation failure** · outcome: grounded · retrieval similarity 0.47 · sufficiency 0.0 · type question
- expected: faq-001 (What services does Pawfect Grooming Studio offer?: Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific)
- retrieved FAQ: [('faq-004', 0.47), ('faq-001', 0.47), ('faq-008', 0.46)] · passages: [('pawfect-handbook-006', 0.42), ('pawfect-handbook-004', 0.33), ('pawfect-handbook-005', 0.3)]
- check of "Do you offer nail clipping as part of grooming" against faq-001: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.41, asks for nothing specific, detail present: True; best sentence: "Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific haircuts."

### 3. Which grooming treatments can I book?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.59 · sufficiency 0.0 · type question
- expected: faq-001 (What services does Pawfect Grooming Studio offer?: Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific)
- retrieved FAQ: [('faq-001', 0.59), ('faq-004', 0.54), ('faq-007', 0.54)] · passages: [('pawfect-handbook-001', 0.5), ('pawfect-handbook-005', 0.43), ('pawfect-handbook-002', 0.42)]
- check of "Which grooming treatments can I book" against faq-001: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.54, asks for nothing specific, detail present: True; best sentence: "Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific haircuts."
- reason given: The information I have does not answer this. Nothing available covers: Which grooming treatments can I book. Could you tell me which of these you mean, or would you like me to pass your question to a 

### 4. I need a bath and trim for my dog; what kind of work do you do?
- class: **partial evidence** · outcome: insufficient · retrieval similarity 0.44 · sufficiency 0.5 · type question
- expected: faq-001 (What services does Pawfect Grooming Studio offer?: Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific)
- retrieved FAQ: [('faq-001', 0.44), ('faq-004', 0.44), ('faq-008', 0.33)] · passages: [('pawfect-handbook-005', 0.42), ('pawfect-handbook-001', 0.36), ('pawfect-handbook-002', 0.31)]
- check of "I need a bath and trim for my dog" against faq-001: words covered 1.0, meaning-assisted 1.0, similarity of best sentence 0.54, asks for nothing specific, detail present: True; best sentence: "Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific haircuts."
- check of "what kind of work do you do" against faq-001: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.13, asks for nothing specific, detail present: True; best sentence: "Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific haircuts."
- reason given: I can answer only part of this. Nothing available covers: what kind of work do you do. Could you tell me which of these you mean, or would you like me to pass your question to a person?

### 5. Does the salon provide coat brushing?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.44 · sufficiency 0.0 · type question
- expected: faq-001 (What services does Pawfect Grooming Studio offer?: Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific)
- retrieved FAQ: [('faq-004', 0.44), ('faq-001', 0.44), ('faq-007', 0.41)] · passages: [('pawfect-handbook-001', 0.48), ('pawfect-handbook-002', 0.48), ('pawfect-handbook-005', 0.39)]
- check of "Does the salon provide coat brushing" against faq-001: words covered 0.5, meaning-assisted 0.5, similarity of best sentence 0.42, asks for nothing specific, detail present: True; best sentence: "Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific haircuts."
- reason given: The information I have does not answer this. Nothing available covers: Does the salon provide coat brushing. Could you tell me which of these you mean, or would you like me to pass your question to a 

### 6. Can I book a nail trim without asking for other services?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.36 · sufficiency 0.0 · type question
- expected: faq-001 (What services does Pawfect Grooming Studio offer?: Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific)
- retrieved FAQ: [('faq-005', 0.36), ('faq-001', 0.34), ('faq-003', 0.28)] · passages: [('pawfect-handbook-006', 0.45), ('pawfect-handbook-002', 0.21), ('pawfect-handbook-004', 0.2)]
- check of "Can I book a nail trim without asking for other services" against faq-001: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.21, asks for nothing specific, detail present: True; best sentence: "Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific haircuts."
- reason given: The information I have does not answer this. Nothing available covers: Can I book a nail trim without asking for other services. Could you tell me which of these you mean, or would you like me to pass

### 7. What grooming options are available for dogs?
- class: **response generation failure** · outcome: grounded · retrieval similarity 0.74 · sufficiency 0.0 · type question
- expected: faq-001 (What services does Pawfect Grooming Studio offer?: Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific)
- retrieved FAQ: [('faq-004', 0.74), ('faq-001', 0.61), ('faq-008', 0.61)] · passages: [('pawfect-handbook-001', 0.51), ('pawfect-handbook-002', 0.48), ('pawfect-handbook-004', 0.47)]
- check of "What grooming options are available for dogs" against faq-001: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.66, asks for nothing specific, detail present: True; best sentence: "Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific haircuts."

### 8. Which part of Pune is the salon in?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.48 · sufficiency 0.0 · type question
- expected: faq-002 (Where is Pawfect Grooming Studio located?: Pawfect Grooming Studio is located in Viman Nagar, Pune.)
- retrieved FAQ: [('faq-002', 0.48), ('faq-003', 0.37), ('faq-001', 0.28)] · passages: [('pawfect-handbook-005', 0.17), ('pawfect-handbook-007', 0.16), ('pawfect-handbook-006', 0.13)]
- check of "Which part of Pune is the salon in" against faq-002: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.49, asks for ['place'], detail present: True; best sentence: "Pawfect Grooming Studio is located in Viman Nagar, Pune."
- reason given: The information I have does not answer this. Nothing available covers: Which part of Pune is the salon in. Could you tell me which of these you mean, or would you like me to pass your question to a pe

### 9. Is the grooming place in Viman Nagar?
- class: **retrieval failure** · outcome: faq · retrieval similarity 0.73 · sufficiency 1.0 · type question
- expected: faq-002 (Where is Pawfect Grooming Studio located?: Pawfect Grooming Studio is located in Viman Nagar, Pune.)
- retrieved FAQ: [('faq-003', 0.73), ('faq-002', 0.62), ('faq-001', 0.44)] · passages: [('pawfect-handbook-001', 0.27), ('pawfect-handbook-005', 0.26), ('pawfect-handbook-006', 0.25)]
- check of "Is the grooming place in Viman Nagar" against faq-002: words covered 0.67, meaning-assisted 0.75, similarity of best sentence 0.66, asks for nothing specific, detail present: True; best sentence: "Pawfect Grooming Studio is located in Viman Nagar, Pune."

### 10. Where should I bring my dog for the appointment?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.46 · sufficiency 0.0 · type question
- expected: faq-002 (Where is Pawfect Grooming Studio located?: Pawfect Grooming Studio is located in Viman Nagar, Pune.)
- retrieved FAQ: [('faq-005', 0.46), ('faq-003', 0.4), ('faq-008', 0.39)] · passages: [('pawfect-handbook-002', 0.49), ('pawfect-handbook-003', 0.41), ('pawfect-handbook-001', 0.38)]
- check of "Where should I bring my dog for the appointment" against faq-002: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.3, asks for ['place'], detail present: True; best sentence: "Pawfect Grooming Studio is located in Viman Nagar, Pune."
- reason given: The information I have does not answer this. Nothing available covers: Where should I bring my dog for the appointment. Could you tell me which of these you mean, or would you like me to pass your que

### 11. What neighbourhood is Pawfect based in?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.66 · sufficiency 0.0 · type question
- expected: faq-002 (Where is Pawfect Grooming Studio located?: Pawfect Grooming Studio is located in Viman Nagar, Pune.)
- retrieved FAQ: [('faq-002', 0.66), ('faq-001', 0.49), ('faq-004', 0.46)] · passages: [('pawfect-handbook-001', 0.16), ('pawfect-handbook-002', 0.14), ('pawfect-handbook-004', 0.13)]
- check of "What neighbourhood is Pawfect based in" against faq-002: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.63, asks for ['place'], detail present: True; best sentence: "Pawfect Grooming Studio is located in Viman Nagar, Pune."
- reason given: The information I have does not answer this. Nothing available covers: What neighbourhood is Pawfect based in. Could you tell me which of these you mean, or would you like me to pass your question to 

### 12. Can you share the area where the shop is situated?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.49 · sufficiency 0.0 · type question
- expected: faq-002 (Where is Pawfect Grooming Studio located?: Pawfect Grooming Studio is located in Viman Nagar, Pune.)
- retrieved FAQ: [('faq-002', 0.49), ('faq-003', 0.37), ('faq-001', 0.23)] · passages: [('pawfect-handbook-006', 0.21), ('pawfect-handbook-005', 0.19), ('pawfect-handbook-004', 0.19)]
- check of "Can you share the area where the shop is situated" against faq-002: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.36, asks for ['place'], detail present: True; best sentence: "Pawfect Grooming Studio is located in Viman Nagar, Pune."
- reason given: The information I have does not answer this. Nothing available covers: Can you share the area where the shop is situated. Could you tell me which of these you mean, or would you like me to pass your q

### 13. Where is the grooming centre?
- class: **retrieval failure** · outcome: faq · retrieval similarity 0.7 · sufficiency 1.0 · type question
- expected: faq-002 (Where is Pawfect Grooming Studio located?: Pawfect Grooming Studio is located in Viman Nagar, Pune.)
- retrieved FAQ: [('faq-002', 0.7), ('faq-003', 0.58), ('faq-001', 0.5)] · passages: [('pawfect-handbook-006', 0.33), ('pawfect-handbook-005', 0.29), ('pawfect-handbook-001', 0.27)]
- check of "Where is the grooming centre" against faq-002: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.57, asks for ['place'], detail present: True; best sentence: "Pawfect Grooming Studio is located in Viman Nagar, Pune."

### 14. How can I reserve a slot?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.31 · sufficiency 0.0 · type question
- expected: faq-005 (How do I book an appointment?: Book by calling 9000012345 or sending a WhatsApp message to the same number.)
- retrieved FAQ: [('faq-005', 0.31), ('faq-009', 0.21), ('faq-010', 0.14)] · passages: [('pawfect-handbook-006', 0.28), ('pawfect-handbook-003', 0.22), ('pawfect-handbook-007', 0.17)]
- check of "How can I reserve a slot" against faq-005: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.21, asks for nothing specific, detail present: True; best sentence: "Book by calling 9000012345 or sending a WhatsApp message to the same number."
- reason given: The information I have does not answer this. Nothing available covers: How can I reserve a slot. Could you tell me which of these you mean, or would you like me to pass your question to a person?

### 15. Can I just walk in with my dog?
- class: **response generation failure** · outcome: grounded · retrieval similarity 0.58 · sufficiency 0.0 · type question
- expected: faq-005 (How do I book an appointment?: Book by calling 9000012345 or sending a WhatsApp message to the same number.)
- retrieved FAQ: [('faq-008', 0.58), ('faq-006', 0.34), ('faq-004', 0.31)] · passages: [('pawfect-handbook-002', 0.44), ('pawfect-handbook-003', 0.33), ('pawfect-handbook-001', 0.32)]
- check of "Can I just walk in with my dog" against faq-005: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.05, asks for nothing specific, detail present: True; best sentence: "Book by calling 9000012345 or sending a WhatsApp message to the same number."

### 16. Do you take customers without prior booking?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.42 · sufficiency 0.0 · type question
- expected: faq-005 (How do I book an appointment?: Book by calling 9000012345 or sending a WhatsApp message to the same number.)
- retrieved FAQ: [('faq-010', 0.42), ('faq-005', 0.42), ('faq-003', 0.37)] · passages: [('pawfect-handbook-003', 0.4), ('pawfect-handbook-007', 0.27), ('pawfect-handbook-006', 0.25)]
- check of "Do you take customers without prior booking" against faq-005: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.2, asks for nothing specific, detail present: True; best sentence: "Book by calling 9000012345 or sending a WhatsApp message to the same number."
- reason given: The information I have does not answer this. Nothing available covers: Do you take customers without prior booking. Could you tell me which of these you mean, or would you like me to pass your questio

### 17. Should I call before coming?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.43 · sufficiency 0.0 · type question
- expected: faq-005 (How do I book an appointment?: Book by calling 9000012345 or sending a WhatsApp message to the same number.)
- retrieved FAQ: [('faq-006', 0.43), ('faq-005', 0.24), ('faq-010', 0.24)] · passages: [('pawfect-handbook-003', 0.26), ('pawfect-handbook-002', 0.19), ('pawfect-handbook-006', 0.19)]
- check of "Should I call before coming" against faq-005: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.22, asks for nothing specific, detail present: True; best sentence: "Book by calling 9000012345 or sending a WhatsApp message to the same number."
- reason given: The information I have does not answer this. Nothing available covers: Should I call before coming. Could you tell me which of these you mean, or would you like me to pass your question to a person?

### 18. Can I send a WhatsApp to arrange grooming?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.43 · sufficiency 0.0 · type question
- expected: faq-005 (How do I book an appointment?: Book by calling 9000012345 or sending a WhatsApp message to the same number.)
- retrieved FAQ: [('faq-008', 0.43), ('faq-003', 0.4), ('faq-001', 0.39)] · passages: [('pawfect-handbook-001', 0.33), ('pawfect-handbook-006', 0.32), ('pawfect-handbook-002', 0.26)]
- check of "Can I send a WhatsApp to arrange grooming" against faq-005: words covered 0.5, meaning-assisted 0.75, similarity of best sentence 0.45, asks for ['contact'], detail present: True; best sentence: "Book by calling 9000012345 or sending a WhatsApp message to the same number."
- reason given: The information I have does not answer this. Nothing available covers: Can I send a WhatsApp to arrange grooming. Could you tell me which of these you mean, or would you like me to pass your question 

### 19. What number should I use to make a booking?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.49 · sufficiency 0.0 · type question
- expected: faq-005 (How do I book an appointment?: Book by calling 9000012345 or sending a WhatsApp message to the same number.)
- retrieved FAQ: [('faq-005', 0.49), ('faq-009', 0.23), ('faq-010', 0.2)] · passages: [('pawfect-handbook-007', 0.23), ('pawfect-handbook-003', 0.18), ('pawfect-handbook-004', 0.16)]
- check of "What number should I use to make a booking" against faq-005: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.36, asks for nothing specific, detail present: True; best sentence: "Book by calling 9000012345 or sending a WhatsApp message to the same number."
- reason given: The information I have does not answer this. Nothing available covers: What number should I use to make a booking. Could you tell me which of these you mean, or would you like me to pass your question

### 20. Is advance appointment booking compulsory?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.54 · sufficiency 0.0 · type question
- expected: faq-005 (How do I book an appointment?: Book by calling 9000012345 or sending a WhatsApp message to the same number.)
- retrieved FAQ: [('faq-005', 0.54), ('faq-006', 0.48), ('faq-010', 0.47)] · passages: [('pawfect-handbook-007', 0.37), ('pawfect-handbook-003', 0.36), ('pawfect-handbook-006', 0.22)]
- check of "Is advance appointment booking compulsory" against faq-005: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.11, asks for nothing specific, detail present: True; best sentence: "Book by calling 9000012345 or sending a WhatsApp message to the same number."
- reason given: The information I have does not answer this. Nothing available covers: Is advance appointment booking compulsory. Could you tell me which of these you mean, or would you like me to pass your question 

### 21. How much time should I set aside for grooming?
- class: **missing evidence** · outcome: insufficient · retrieval similarity 0.78 · sufficiency 0.0 · type question
- expected: faq-007 (How long does a grooming session take?: A standard grooming appointment usually takes 60 to 90 minutes, depending on the dog's coa)
- retrieved FAQ: [('faq-007', 0.78), ('faq-008', 0.51), ('faq-004', 0.43)] · passages: [('pawfect-handbook-004', 0.37), ('pawfect-handbook-001', 0.35), ('pawfect-handbook-005', 0.32)]
- check of "How much time should I set aside for grooming" against faq-007: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.64, asks for ['price'], detail present: False; best sentence: "A standard grooming appointment usually takes 60 to 90 minutes, depending on the dog's coat and service."
- reason given: The information I have does not answer this. Nothing available covers: How much time should I set aside for grooming. Could you tell me which of these you mean, or would you like me to pass your quest

### 22. Will a regular session take around an hour?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.6 · sufficiency 0.0 · type question
- expected: faq-007 (How long does a grooming session take?: A standard grooming appointment usually takes 60 to 90 minutes, depending on the dog's coa)
- retrieved FAQ: [('faq-007', 0.6), ('faq-010', 0.26), ('faq-006', 0.23)] · passages: [('pawfect-handbook-007', 0.32), ('pawfect-handbook-003', 0.29), ('pawfect-handbook-004', 0.07)]
- check of "Will a regular session take around an hour" against faq-007: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.34, asks for nothing specific, detail present: True; best sentence: "A standard grooming appointment usually takes 60 to 90 minutes, depending on the dog's coat and service."
- reason given: The information I have does not answer this. Nothing available covers: Will a regular session take around an hour. Could you tell me which of these you mean, or would you like me to pass your question

### 23. How long might my pet be at the studio?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.41 · sufficiency 0.0 · type question
- expected: faq-007 (How long does a grooming session take?: A standard grooming appointment usually takes 60 to 90 minutes, depending on the dog's coa)
- retrieved FAQ: [('faq-002', 0.41), ('faq-007', 0.38), ('faq-004', 0.35)] · passages: [('pawfect-handbook-003', 0.51), ('pawfect-handbook-007', 0.44), ('pawfect-handbook-001', 0.39)]
- check of "How long might my pet be at the studio" against faq-007: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.42, asks for ['duration'], detail present: True; best sentence: "A standard grooming appointment usually takes 60 to 90 minutes, depending on the dog's coat and service."
- reason given: The information I have does not answer this. Nothing available covers: How long might my pet be at the studio. Could you tell me which of these you mean, or would you like me to pass your question to 

### 24. What's the usual appointment length?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.52 · sufficiency 0.0 · type question
- expected: faq-007 (How long does a grooming session take?: A standard grooming appointment usually takes 60 to 90 minutes, depending on the dog's coa)
- retrieved FAQ: [('faq-007', 0.52), ('faq-005', 0.45), ('faq-010', 0.39)] · passages: [('pawfect-handbook-007', 0.43), ('pawfect-handbook-003', 0.33), ('pawfect-handbook-006', 0.19)]
- check of "What's the usual appointment length" against faq-007: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.52, asks for nothing specific, detail present: True; best sentence: "A standard grooming appointment usually takes 60 to 90 minutes, depending on the dog's coat and service."
- reason given: The information I have does not answer this. Nothing available covers: What's the usual appointment length. Could you tell me which of these you mean, or would you like me to pass your question to a p

### 25. Is cash okay for the grooming bill?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.35 · sufficiency 0.0 · type question
- expected: faq-009 (What payment methods do you accept?: Pawfect accepts UPI, cash, and debit or credit cards.)
- retrieved FAQ: [('faq-009', 0.35), ('faq-008', 0.34), ('faq-001', 0.3)] · passages: [('pawfect-handbook-006', 0.37), ('pawfect-handbook-003', 0.25), ('pawfect-handbook-005', 0.24)]
- check of "Is cash okay for the grooming bill" against faq-009: words covered 0.25, meaning-assisted 0.25, similarity of best sentence 0.31, asks for nothing specific, detail present: True; best sentence: "Pawfect accepts UPI, cash, and debit or credit cards."
- reason given: The information I have does not answer this. Nothing available covers: Is cash okay for the grooming bill. Could you tell me which of these you mean, or would you like me to pass your question to a pe

### 26. Which payment options are available?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.75 · sufficiency 0.0 · type question
- expected: faq-009 (What payment methods do you accept?: Pawfect accepts UPI, cash, and debit or credit cards.)
- retrieved FAQ: [('faq-009', 0.75), ('faq-001', 0.24), ('faq-010', 0.18)] · passages: [('pawfect-handbook-006', 0.26), ('pawfect-handbook-003', 0.17), ('pawfect-handbook-007', 0.05)]
- check of "Which payment options are available" against faq-009: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.5, asks for nothing specific, detail present: True; best sentence: "Pawfect accepts UPI, cash, and debit or credit cards."
- reason given: The information I have does not answer this. Nothing available covers: Which payment options are available. Could you tell me which of these you mean, or would you like me to pass your question to a p

### 27. How early should I inform you if I cannot come?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.34 · sufficiency 0.0 · type question
- expected: faq-010 (Can I cancel or reschedule?: Please give at least 6 hours' notice to cancel or reschedule an appointment.)
- retrieved FAQ: [('faq-006', 0.34), ('faq-010', 0.33), ('faq-003', 0.22)] · passages: [('pawfect-handbook-003', 0.35), ('pawfect-handbook-007', 0.32), ('pawfect-handbook-002', 0.17)]
- check of "How early should I inform you if I cannot come" against faq-010: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.39, asks for nothing specific, detail present: True; best sentence: "Please give at least 6 hours' notice to cancel or reschedule an appointment."
- reason given: The information I have does not answer this. Nothing available covers: How early should I inform you if I cannot come. Could you tell me which of these you mean, or would you like me to pass your ques

### 28. Can I move my slot to another day?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.4 · sufficiency 0.0 · type question
- expected: faq-010 (Can I cancel or reschedule?: Please give at least 6 hours' notice to cancel or reschedule an appointment.)
- retrieved FAQ: [('faq-010', 0.4), ('faq-006', 0.2), ('faq-005', 0.19)] · passages: [('pawfect-handbook-007', 0.27), ('pawfect-handbook-003', 0.2), ('pawfect-handbook-006', 0.15)]
- check of "Can I move my slot to another day" against faq-010: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.26, asks for nothing specific, detail present: True; best sentence: "Please give at least 6 hours' notice to cancel or reschedule an appointment."
- reason given: The information I have does not answer this. Nothing available covers: Can I move my slot to another day. Could you tell me which of these you mean, or would you like me to pass your question to a per

### 29. What if my pet has an open wound?
- class: **missing evidence** · outcome: insufficient · retrieval similarity 0.36 · sufficiency 0.0 · type ambiguous
- expected: pawfect-handbook-001 (Coat and skin preparation: Please tell the groomer about any known skin irritation, recent surgery, allergies, or sen); pawfect-handbook-002 (Coat and skin preparation: Dogs should arrive on a leash. Please bring any medication or care instructions that the g)
- retrieved FAQ: [('faq-004', 0.36), ('faq-008', 0.35), ('faq-001', 0.3)] · passages: [('pawfect-handbook-001', 0.58), ('pawfect-handbook-002', 0.49), ('pawfect-handbook-004', 0.43)]
- check of "What if my pet has an open wound" against pawfect-handbook-001: words covered 0.67, meaning-assisted 0.75, similarity of best sentence 0.72, asks for ['time'], detail present: False; best sentence: "If a dog has open wounds, a contagious skin condition, or appears unwell, the appointment may be postponed and the owner may be asked to con"
- check of "What if my pet has an open wound" against pawfect-handbook-002: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.47, asks for ['time'], detail present: False; best sentence: "Dogs should arrive on a leash."
- reason given: The information I have does not answer this. Nothing available covers: What if my pet has an open wound. Could you tell me which of these you mean, or would you like me to pass your question to a pers

### 30. My dog seems sick today; can the session go ahead?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.42 · sufficiency 0.0 · type ambiguous
- expected: pawfect-handbook-001 (Coat and skin preparation: Please tell the groomer about any known skin irritation, recent surgery, allergies, or sen); pawfect-handbook-002 (Coat and skin preparation: Dogs should arrive on a leash. Please bring any medication or care instructions that the g)
- retrieved FAQ: [('faq-008', 0.42), ('faq-006', 0.31), ('faq-007', 0.29)] · passages: [('pawfect-handbook-001', 0.51), ('pawfect-handbook-002', 0.42), ('pawfect-handbook-004', 0.39)]
- check of "My dog seems sick today" against pawfect-handbook-001: words covered 0.25, meaning-assisted 0.25, similarity of best sentence 0.48, asks for nothing specific, detail present: True; best sentence: "If a dog has open wounds, a contagious skin condition, or appears unwell, the appointment may be postponed and the owner may be asked to con"
- check of "My dog seems sick today" against pawfect-handbook-002: words covered 0.25, meaning-assisted 0.25, similarity of best sentence 0.35, asks for nothing specific, detail present: True; best sentence: "Dogs should arrive on a leash."
- check of "can the session go ahead" against pawfect-handbook-001: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.19, asks for nothing specific, detail present: True; best sentence: "Please tell the groomer about any known skin irritation, recent surgery, allergies, or sensitivity before the session begins."
- reason given: The information I have does not answer this. Nothing available covers: My dog seems sick today; can the session go ahead. Could you tell me which of these you mean, or would you like me to pass your q

### 31. What information should I share about a recent operation?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.2 · sufficiency 0.0 · type ambiguous
- expected: pawfect-handbook-001 (Coat and skin preparation: Please tell the groomer about any known skin irritation, recent surgery, allergies, or sen); pawfect-handbook-002 (Coat and skin preparation: Dogs should arrive on a leash. Please bring any medication or care instructions that the g)
- retrieved FAQ: [('faq-003', 0.2), ('faq-005', 0.15), ('faq-006', 0.13)] · passages: [('pawfect-handbook-006', 0.23), ('pawfect-handbook-003', 0.22), ('pawfect-handbook-005', 0.2)]
- check of "What information should I share about a recent operation" against pawfect-handbook-001: words covered 0.25, meaning-assisted 0.25, similarity of best sentence 0.25, asks for nothing specific, detail present: True; best sentence: "Please tell the groomer about any known skin irritation, recent surgery, allergies, or sensitivity before the session begins."
- check of "What information should I share about a recent operation" against pawfect-handbook-002: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.03, asks for nothing specific, detail present: True; best sentence: "Dogs should arrive on a leash."
- reason given: The information I have does not answer this. Nothing available covers: What information should I share about a recent operation. Could you tell me which of these you mean, or would you like me to pass

### 32. What happens if I arrive twenty minutes late?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.29 · sufficiency 0.0 · type question
- expected: pawfect-handbook-003 (Late arrival and collection: If an owner is more than 15 minutes late, the studio may shorten the service or offer the )
- retrieved FAQ: [('faq-010', 0.29), ('faq-006', 0.22), ('faq-007', 0.14)] · passages: [('pawfect-handbook-003', 0.52), ('pawfect-handbook-007', 0.19), ('pawfect-handbook-004', 0.11)]
- check of "What happens if I arrive twenty minutes late" against pawfect-handbook-003: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.47, asks for nothing specific, detail present: True; best sentence: "If an owner is more than 15 minutes late, the studio may shorten the service or offer the next available appointment."
- reason given: The information I have does not answer this. Nothing available covers: What happens if I arrive twenty minutes late. Could you tell me which of these you mean, or would you like me to pass your questi

### 33. How long after the completion message can I pick up my pet?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.43 · sufficiency 0.0 · type question
- expected: pawfect-handbook-003 (Late arrival and collection: If an owner is more than 15 minutes late, the studio may shorten the service or offer the )
- retrieved FAQ: [('faq-007', 0.43), ('faq-004', 0.31), ('faq-008', 0.3)] · passages: [('pawfect-handbook-003', 0.62), ('pawfect-handbook-004', 0.37), ('pawfect-handbook-002', 0.33)]
- check of "How long after the completion message can I pick up my pet" against pawfect-handbook-003: words covered 0.29, meaning-assisted 0.29, similarity of best sentence 0.78, asks for ['duration'], detail present: True; best sentence: "Dogs should be collected within 30 minutes of the completion message."
- reason given: The information I have does not answer this. Nothing available covers: How long after the completion message can I pick up my pet. Could you tell me which of these you mean, or would you like me to pa

### 34. What if traffic makes me late for the appointment?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.46 · sufficiency 0.0 · type question
- expected: pawfect-handbook-003 (Late arrival and collection: If an owner is more than 15 minutes late, the studio may shorten the service or offer the )
- retrieved FAQ: [('faq-006', 0.46), ('faq-010', 0.45), ('faq-005', 0.4)] · passages: [('pawfect-handbook-003', 0.4), ('pawfect-handbook-007', 0.3), ('pawfect-handbook-006', 0.24)]
- check of "What if traffic makes me late for the appointment" against pawfect-handbook-003: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.57, asks for nothing specific, detail present: True; best sentence: "If an owner is more than 15 minutes late, the studio may shorten the service or offer the next available appointment."
- reason given: The information I have does not answer this. Nothing available covers: What if traffic makes me late for the appointment. Could you tell me which of these you mean, or would you like me to pass your q

### 35. How is the late pickup charge calculated?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.14 · sufficiency 0.0 · type question
- expected: pawfect-handbook-003 (Late arrival and collection: If an owner is more than 15 minutes late, the studio may shorten the service or offer the )
- retrieved FAQ: [('faq-009', 0.14), ('faq-005', 0.13), ('faq-007', 0.13)] · passages: [('pawfect-handbook-003', 0.43), ('pawfect-handbook-006', 0.1), ('pawfect-handbook-007', 0.1)]
- check of "How is the late pickup charge calculated" against pawfect-handbook-003: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.49, asks for ['price'], detail present: True; best sentence: "A late collection fee of ₹100 applies for each additional 30-minute period, subject to staff availability."
- reason given: The information I have does not answer this. Nothing available covers: How is the late pickup charge calculated. Could you tell me which of these you mean, or would you like me to pass your question t

### 36. Will you put a muzzle on my dog automatically?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.41 · sufficiency 0.0 · type ambiguous
- expected: pawfect-handbook-004 (Safety and handling: For the safety of pets and staff, grooming may be paused if a dog shows severe distress or); pawfect-handbook-005 (Safety and handling: The studio uses separate, cleaned grooming tools for each appointment. Tables, tubs, and c)
- retrieved FAQ: [('faq-008', 0.41), ('faq-004', 0.27), ('faq-001', 0.18)] · passages: [('pawfect-handbook-004', 0.59), ('pawfect-handbook-002', 0.43), ('pawfect-handbook-001', 0.37)]
- check of "Will you put a muzzle on my dog automatically" against pawfect-handbook-004: words covered 0.25, meaning-assisted 0.25, similarity of best sentence 0.32, asks for nothing specific, detail present: True; best sentence: "For the safety of pets and staff, grooming may be paused if a dog shows severe distress or repeated attempts to bite."
- check of "Will you put a muzzle on my dog automatically" against pawfect-handbook-005: words covered 0.25, meaning-assisted 0.25, similarity of best sentence 0.2, asks for nothing specific, detail present: True; best sentence: "Tables, tubs, and contact surfaces are cleaned and disinfected between dogs."
- reason given: The information I have does not answer this. Nothing available covers: Will you put a muzzle on my dog automatically. Could you tell me which of these you mean, or would you like me to pass your quest

### 37. When does the last appointment begin?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.54 · sufficiency 0.0 · type question
- expected: pawfect-handbook-007 (Opening schedule: The studio is open Tuesday to Sunday, 9:00 AM to 7:00 PM. It is closed on Mondays. The fin)
- retrieved FAQ: [('faq-005', 0.54), ('faq-006', 0.47), ('faq-010', 0.41)] · passages: [('pawfect-handbook-007', 0.54), ('pawfect-handbook-003', 0.26), ('pawfect-handbook-006', 0.22)]
- check of "When does the last appointment begin" against pawfect-handbook-007: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.81, asks for ['time'], detail present: True; best sentence: "The final appointment slot starts at 5:30 PM."
- reason given: The information I have does not answer this. Nothing available covers: When does the last appointment begin. Could you tell me which of these you mean, or would you like me to pass your question to a 

### 38. What are your normal weekly operating days?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.33 · sufficiency 0.0 · type question
- expected: pawfect-handbook-007 (Opening schedule: The studio is open Tuesday to Sunday, 9:00 AM to 7:00 PM. It is closed on Mondays. The fin)
- retrieved FAQ: [('faq-007', 0.33), ('faq-010', 0.25), ('faq-003', 0.25)] · passages: [('pawfect-handbook-007', 0.41), ('pawfect-handbook-005', 0.12), ('pawfect-handbook-003', 0.12)]
- check of "What are your normal weekly operating days" against pawfect-handbook-007: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.33, asks for nothing specific, detail present: True; best sentence: "The studio is open Tuesday to Sunday, 9:00 AM to 7:00 PM."
- reason given: The information I have does not answer this. Nothing available covers: What are your normal weekly operating days. Could you tell me which of these you mean, or would you like me to pass your question

### 39. When should I raise a concern about the grooming?
- class: **missing evidence** · outcome: insufficient · retrieval similarity 0.62 · sufficiency 0.0 · type question
- expected: pawfect-handbook-006 (Pick-up checklist: At collection, the owner should check the coat, nails, and any requested grooming areas wi)
- retrieved FAQ: [('faq-007', 0.62), ('faq-008', 0.59), ('faq-004', 0.51)] · passages: [('pawfect-handbook-001', 0.51), ('pawfect-handbook-004', 0.48), ('pawfect-handbook-006', 0.43)]
- check of "When should I raise a concern about the grooming" against pawfect-handbook-006: words covered 0.67, meaning-assisted 0.75, similarity of best sentence 0.48, asks for ['time'], detail present: False; best sentence: "At collection, the owner should check the coat, nails, and any requested grooming areas with the staff member. Please report a concern befor"
- reason given: The information I have does not answer this. Nothing available covers: When should I raise a concern about the grooming. Could you tell me which of these you mean, or would you like me to pass your qu

### 40. Will you take my dog's picture without asking?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.41 · sufficiency 0.0 · type question
- expected: pawfect-handbook-006 (Pick-up checklist: At collection, the owner should check the coat, nails, and any requested grooming areas wi)
- retrieved FAQ: [('faq-008', 0.41), ('faq-004', 0.35), ('faq-003', 0.24)] · passages: [('pawfect-handbook-001', 0.36), ('pawfect-handbook-006', 0.35), ('pawfect-handbook-003', 0.32)]
- check of "Will you take my dog's picture without asking" against pawfect-handbook-006: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.2, asks for nothing specific, detail present: True; best sentence: "At collection, the owner should check the coat, nails, and any requested grooming areas with the staff member."
- reason given: The information I have does not answer this. Nothing available covers: Will you take my dog's picture without asking. Could you tell me which of these you mean, or would you like me to pass your quest

### 41. If I arrive 18 minutes after my slot, what might happen?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.24 · sufficiency 0.0 · type question
- expected: pawfect-handbook-003 (Late arrival and collection: If an owner is more than 15 minutes late, the studio may shorten the service or offer the )
- retrieved FAQ: [('faq-010', 0.24), ('faq-006', 0.22), ('faq-007', 0.18)] · passages: [('pawfect-handbook-003', 0.45), ('pawfect-handbook-007', 0.23), ('pawfect-handbook-006', 0.11)]
- check of "If I arrive 18 minutes after my slot" against pawfect-handbook-003: words covered 0.25, meaning-assisted 0.25, similarity of best sentence 0.4, asks for nothing specific, detail present: True; best sentence: "If an owner is more than 15 minutes late, the studio may shorten the service or offer the next available appointment."
- reason given: The information I have does not answer this. Nothing available covers: If I arrive 18 minutes after my slot. Could you tell me which of these you mean, or would you like me to pass your question to a 

### 42. My dog has allergies and a recent surgery. What should I tell the team?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.23 · sufficiency 0.0 · type ambiguous
- expected: pawfect-handbook-001 (Coat and skin preparation: Please tell the groomer about any known skin irritation, recent surgery, allergies, or sen); pawfect-handbook-002 (Coat and skin preparation: Dogs should arrive on a leash. Please bring any medication or care instructions that the g)
- retrieved FAQ: [('faq-001', 0.23), ('faq-008', 0.22), ('faq-004', 0.2)] · passages: [('pawfect-handbook-001', 0.49), ('pawfect-handbook-002', 0.36), ('pawfect-handbook-004', 0.29)]
- check of "My dog has allergies and a recent surgery. What should I tell the team" against pawfect-handbook-001: words covered 0.6, meaning-assisted 0.6, similarity of best sentence 0.39, asks for nothing specific, detail present: True; best sentence: "Please tell the groomer about any known skin irritation, recent surgery, allergies, or sensitivity before the session begins."
- check of "My dog has allergies and a recent surgery. What should I tell the team" against pawfect-handbook-002: words covered 0.2, meaning-assisted 0.2, similarity of best sentence 0.3, asks for nothing specific, detail present: True; best sentence: "Dogs should arrive on a leash."
- reason given: The information I have does not answer this. Nothing available covers: My dog has allergies and a recent surgery. What should I tell the team. Could you tell me which of these you mean, or would you l

### 43. Can I book for a cat and pay by card?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.48 · sufficiency 0.0 · type ambiguous
- expected: faq-004 (Which pets do you groom?: Pawfect currently accepts dogs only. It does not groom cats or other animals.); faq-009 (What payment methods do you accept?: Pawfect accepts UPI, cash, and debit or credit cards.)
- retrieved FAQ: [('faq-009', 0.48), ('faq-005', 0.3), ('faq-004', 0.29)] · passages: [('pawfect-handbook-003', 0.33), ('pawfect-handbook-006', 0.17), ('pawfect-handbook-002', 0.14)]
- check of "Can I book for a cat and pay by card" against faq-004: words covered 0.25, meaning-assisted 0.25, similarity of best sentence 0.27, asks for nothing specific, detail present: True; best sentence: "It does not groom cats or other animals."
- check of "Can I book for a cat and pay by card" against faq-009: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.53, asks for nothing specific, detail present: True; best sentence: "Pawfect accepts UPI, cash, and debit or credit cards."
- reason given: The information I have does not answer this. Nothing available covers: Can I book for a cat and pay by card. Could you tell me which of these you mean, or would you like me to pass your question to a 

### 44. Can I collect my dog 45 minutes after the completion message, and is there an extra charge?
- class: **partial evidence** · outcome: insufficient · retrieval similarity 0.34 · sufficiency 0.0 · type question
- expected: pawfect-handbook-003 (Late arrival and collection: If an owner is more than 15 minutes late, the studio may shorten the service or offer the )
- retrieved FAQ: [('faq-010', 0.34), ('faq-007', 0.31), ('faq-008', 0.3)] · passages: [('pawfect-handbook-003', 0.71), ('pawfect-handbook-002', 0.3), ('pawfect-handbook-004', 0.3)]
- check of "Can I collect my dog 45 minutes after the completion message" against pawfect-handbook-003: words covered 0.83, meaning-assisted 0.83, similarity of best sentence 0.82, asks for nothing specific, detail present: True; best sentence: "Dogs should be collected within 30 minutes of the completion message."
- check of "is there an extra charge" against pawfect-handbook-003: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.35, asks for ['price'], detail present: True; best sentence: "A late collection fee of ₹100 applies for each additional 30-minute period, subject to staff availability."
- reason given: I can answer only part of this. Nothing available covers: is there an extra charge. Could you tell me which of these you mean, or would you like me to pass your question to a person?

### 45. Can I walk in on a Tuesday morning?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.41 · sufficiency 0.0 · type question
- expected: faq-005 (How do I book an appointment?: Book by calling 9000012345 or sending a WhatsApp message to the same number.)
- retrieved FAQ: [('faq-006', 0.41), ('faq-010', 0.4), ('faq-003', 0.28)] · passages: [('pawfect-handbook-007', 0.4), ('pawfect-handbook-003', 0.17), ('pawfect-handbook-002', 0.13)]
- check of "Can I walk in on a Tuesday morning" against faq-005: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.07, asks for nothing specific, detail present: True; best sentence: "Book by calling 9000012345 or sending a WhatsApp message to the same number."
- reason given: The information I have does not answer this. Nothing available covers: Can I walk in on a Tuesday morning. Could you tell me which of these you mean, or would you like me to pass your question to a pe

### 46. Are grooming tools cleaned between dogs?
- class: **retrieval failure** · outcome: faq · retrieval similarity 0.59 · sufficiency 1.0 · type ambiguous
- expected: pawfect-handbook-004 (Safety and handling: For the safety of pets and staff, grooming may be paused if a dog shows severe distress or); pawfect-handbook-005 (Safety and handling: The studio uses separate, cleaned grooming tools for each appointment. Tables, tubs, and c)
- retrieved FAQ: [('faq-008', 0.59), ('faq-004', 0.58), ('faq-001', 0.51)] · passages: [('pawfect-handbook-005', 0.71), ('pawfect-handbook-001', 0.58), ('pawfect-handbook-004', 0.51)]
- check of "Are grooming tools cleaned between dogs" against pawfect-handbook-004: words covered 0.4, meaning-assisted 0.75, similarity of best sentence 0.54, asks for nothing specific, detail present: True; best sentence: "For the safety of pets and staff, grooming may be paused if a dog shows severe distress or repeated attempts to bite."
- check of "Are grooming tools cleaned between dogs" against pawfect-handbook-005: words covered 1.0, meaning-assisted 1.0, similarity of best sentence 0.73, asks for nothing specific, detail present: True; best sentence: "The studio uses separate, cleaned grooming tools for each appointment. Tables, tubs, and contact surfaces are cleaned and disinfected betwee"

### 47. What should I check before leaving after the appointment?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.47 · sufficiency 0.0 · type question
- expected: pawfect-handbook-006 (Pick-up checklist: At collection, the owner should check the coat, nails, and any requested grooming areas wi)
- retrieved FAQ: [('faq-005', 0.47), ('faq-006', 0.47), ('faq-010', 0.3)] · passages: [('pawfect-handbook-006', 0.41), ('pawfect-handbook-005', 0.22), ('pawfect-handbook-001', 0.21)]
- check of "What should I check before leaving after the appointment" against pawfect-handbook-006: words covered 0.6, meaning-assisted 0.6, similarity of best sentence 0.31, asks for nothing specific, detail present: True; best sentence: "At collection, the owner should check the coat, nails, and any requested grooming areas with the staff member. Please report a concern befor"
- reason given: The information I have does not answer this. Nothing available covers: What should I check before leaving after the appointment. Could you tell me which of these you mean, or would you like me to pass

### 48. Can you guarantee that a nervous dog will finish the full haircut?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.53 · sufficiency 0.0 · type ambiguous
- expected: pawfect-handbook-004 (Safety and handling: For the safety of pets and staff, grooming may be paused if a dog shows severe distress or); pawfect-handbook-005 (Safety and handling: The studio uses separate, cleaned grooming tools for each appointment. Tables, tubs, and c)
- retrieved FAQ: [('faq-008', 0.53), ('faq-004', 0.47), ('faq-007', 0.39)] · passages: [('pawfect-handbook-001', 0.55), ('pawfect-handbook-004', 0.52), ('pawfect-handbook-002', 0.44)]
- check of "Can you guarantee that a nervous dog will finish the full haircut" against pawfect-handbook-004: words covered 0.17, meaning-assisted 0.17, similarity of best sentence 0.61, asks for nothing specific, detail present: True; best sentence: "For the safety of pets and staff, grooming may be paused if a dog shows severe distress or repeated attempts to bite."
- check of "Can you guarantee that a nervous dog will finish the full haircut" against pawfect-handbook-005: words covered 0.17, meaning-assisted 0.17, similarity of best sentence 0.22, asks for nothing specific, detail present: True; best sentence: "Tables, tubs, and contact surfaces are cleaned and disinfected between dogs."
- reason given: The information I have does not answer this. Nothing available covers: Can you guarantee that a nervous dog will finish the full haircut. Could you tell me which of these you mean, or would you like m

### 49. What is the late collection fee for one extra hour?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.31 · sufficiency 0.0 · type question
- expected: pawfect-handbook-003 (Late arrival and collection: If an owner is more than 15 minutes late, the studio may shorten the service or offer the )
- retrieved FAQ: [('faq-010', 0.31), ('faq-009', 0.3), ('faq-005', 0.2)] · passages: [('pawfect-handbook-003', 0.64), ('pawfect-handbook-007', 0.2), ('pawfect-handbook-006', 0.11)]
- check of "What is the late collection fee for one extra hour" against pawfect-handbook-003: words covered 0.25, meaning-assisted 0.25, similarity of best sentence 0.73, asks for ['price'], detail present: True; best sentence: "A late collection fee of ₹100 applies for each additional 30-minute period, subject to staff availability."
- reason given: The information I have does not answer this. Nothing available covers: What is the late collection fee for one extra hour. Could you tell me which of these you mean, or would you like me to pass your 

### 50. Can I book a grooming slot on Monday?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.5 · sufficiency 0.0 · type question
- expected: pawfect-handbook-007 (Opening schedule: The studio is open Tuesday to Sunday, 9:00 AM to 7:00 PM. It is closed on Mondays. The fin)
- retrieved FAQ: [('faq-003', 0.5), ('faq-008', 0.42), ('faq-007', 0.42)] · passages: [('pawfect-handbook-006', 0.38), ('pawfect-handbook-007', 0.38), ('pawfect-handbook-001', 0.28)]
- check of "Can I book a grooming slot on Monday" against pawfect-handbook-007: words covered 0.5, meaning-assisted 0.5, similarity of best sentence 0.44, asks for nothing specific, detail present: True; best sentence: "It is closed on Mondays. The final appointment slot starts at 5:30 PM."
- reason given: The information I have does not answer this. Nothing available covers: Can I book a grooming slot on Monday. Could you tell me which of these you mean, or would you like me to pass your question to a 

### 51. Can I stay in the grooming room while the dog is being washed?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.81 · sufficiency 0.0 · type question
- expected: faq-008 (Can I stay with my dog during grooming?: Owners may stay for the initial handover, but they cannot remain in the grooming area duri)
- retrieved FAQ: [('faq-008', 0.81), ('faq-004', 0.44), ('faq-007', 0.4)] · passages: [('pawfect-handbook-001', 0.54), ('pawfect-handbook-005', 0.51), ('pawfect-handbook-004', 0.46)]
- check of "Can I stay in the grooming room while the dog is being washed" against faq-008: words covered 0.0, meaning-assisted 0.0, similarity of best sentence 0.46, asks for nothing specific, detail present: True; best sentence: "Owners may stay for the initial handover, but they cannot remain in the grooming area during the session."
- reason given: The information I have does not answer this. Nothing available covers: Can I stay in the grooming room while the dog is being washed. Could you tell me which of these you mean, or would you like me to

### 52. Can I use UPI to settle the grooming payment?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.47 · sufficiency 0.0 · type question
- expected: faq-009 (What payment methods do you accept?: Pawfect accepts UPI, cash, and debit or credit cards.)
- retrieved FAQ: [('faq-009', 0.47), ('faq-003', 0.42), ('faq-008', 0.37)] · passages: [('pawfect-handbook-003', 0.28), ('pawfect-handbook-006', 0.26), ('pawfect-handbook-004', 0.25)]
- check of "Can I use UPI to settle the grooming payment" against faq-009: words covered 0.25, meaning-assisted 0.25, similarity of best sentence 0.43, asks for nothing specific, detail present: True; best sentence: "Pawfect accepts UPI, cash, and debit or credit cards."
- reason given: The information I have does not answer this. Nothing available covers: Can I use UPI to settle the grooming payment. Could you tell me which of these you mean, or would you like me to pass your questi

### 53. Is the studio open every day of the week?
- class: **paraphrase mismatch** · outcome: insufficient · retrieval similarity 0.37 · sufficiency 0.0 · type question
- expected: pawfect-handbook-007 (Opening schedule: The studio is open Tuesday to Sunday, 9:00 AM to 7:00 PM. It is closed on Mondays. The fin)
- retrieved FAQ: [('faq-003', 0.37), ('faq-010', 0.33), ('faq-002', 0.3)] · passages: [('pawfect-handbook-007', 0.77), ('pawfect-handbook-003', 0.19), ('pawfect-handbook-005', 0.18)]
- check of "Is the studio open every day of the week" against pawfect-handbook-007: words covered 0.25, meaning-assisted 0.25, similarity of best sentence 0.85, asks for ['time'], detail present: True; best sentence: "The studio is open Tuesday to Sunday, 9:00 AM to 7:00 PM."
- reason given: The information I have does not answer this. Nothing available covers: Is the studio open every day of the week. Could you tell me which of these you mean, or would you like me to pass your question t

### 54. How much notice is needed to reschedule?
- class: **missing evidence** · outcome: insufficient · retrieval similarity 0.54 · sufficiency 0.0 · type question
- expected: faq-010 (Can I cancel or reschedule?: Please give at least 6 hours' notice to cancel or reschedule an appointment.)
- retrieved FAQ: [('faq-010', 0.54), ('faq-006', 0.33), ('faq-005', 0.27)] · passages: [('pawfect-handbook-003', 0.36), ('pawfect-handbook-007', 0.34), ('pawfect-handbook-006', 0.15)]
- check of "How much notice is needed to reschedule" against faq-010: words covered 1.0, meaning-assisted 1.0, similarity of best sentence 0.62, asks for ['price'], detail present: False; best sentence: "Please give at least 6 hours' notice to cancel or reschedule an appointment."
- reason given: The information I have does not answer this. Nothing available covers: How much notice is needed to reschedule. Could you tell me which of these you mean, or would you like me to pass your question to

### 55. If my dog has an allergy, should I mention it before the appointment?
- class: **partial evidence** · outcome: insufficient · retrieval similarity 0.41 · sufficiency 0.0 · type ambiguous
- expected: pawfect-handbook-001 (Coat and skin preparation: Please tell the groomer about any known skin irritation, recent surgery, allergies, or sen); pawfect-handbook-002 (Coat and skin preparation: Dogs should arrive on a leash. Please bring any medication or care instructions that the g)
- retrieved FAQ: [('faq-006', 0.41), ('faq-008', 0.36), ('faq-007', 0.25)] · passages: [('pawfect-handbook-001', 0.58), ('pawfect-handbook-002', 0.45), ('pawfect-handbook-004', 0.34)]
- check of "If my dog has an allergy" against pawfect-handbook-001: words covered 0.5, meaning-assisted 0.75, similarity of best sentence 0.49, asks for nothing specific, detail present: True; best sentence: "If a dog has open wounds, a contagious skin condition, or appears unwell, the appointment may be postponed and the owner may be asked to con"
- check of "If my dog has an allergy" against pawfect-handbook-002: words covered 0.5, meaning-assisted 0.5, similarity of best sentence 0.29, asks for nothing specific, detail present: True; best sentence: "Dogs should arrive on a leash."
- check of "should I mention it before the appointment" against pawfect-handbook-001: words covered 0.33, meaning-assisted 0.33, similarity of best sentence 0.39, asks for nothing specific, detail present: True; best sentence: "Please tell the groomer about any known skin irritation, recent surgery, allergies, or sensitivity before the session begins."
- reason given: I can answer only part of this. Nothing available covers: should I mention it before the appointment. Could you tell me which of these you mean, or would you like me to pass your question to a person?
