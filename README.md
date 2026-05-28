# AI-Powered SKU Recommendation Engine

## Overview
This project is a logic-based recommendation system designed to match current-generation SKUs with next-generation equivalents, while also suggesting in-stock alternatives and upgrade paths based on configuration requirements.

The solution aims to solve a common challenge in sales and solution engineering: quickly identifying comparable or improved products when availability, configuration, or lifecycle constraints impact the original request.

---

## Problem
Sales teams and solution architects often struggle to:
- Identify equivalent SKUs when inventory is unavailable
- Map current-generation to next-generation devices
- Recommend upgrades based on technical requirements (RAM, processor, form factor)
- Provide consistent, scalable recommendations across teams

These challenges result in delayed deal progression and inconsistent customer experiences.

---

## Solution
This system introduces a structured decision-making engine that:

- Maps SKUs based on key attributes (RAM, processor, model tier)
- Recommends in-stock alternatives when requested SKUs are unavailable
- Identifies next-generation equivalents to support lifecycle transitions
- Suggests upgrade paths (e.g., higher RAM configurations) when aligned with customer needs

---

## How It Works

1. User inputs:
   - SKU OR
   - Configuration requirements (e.g., 32GB RAM, device type)

2. System processes:
   - Maps product attributes across generations
   - Compares specifications (RAM, CPU, device family)
   - Applies substitution and upgrade logic

3. Outputs:
   - In-stock alternative SKUs
   - Next-generation equivalents
   - Upgrade recommendations where applicable

---

## Core Features

- ✅ Cross-generation SKU mapping  
- ✅ Inventory-aware recommendations  
- ✅ Configuration-based matching  
- ✅ Upgrade suggestion logic  
- ✅ Scalable, repeatable decision framework  

---

## Example Use Case

**Input:** Surface Laptop 5, 32GB (Out of Stock)  

**Output:**
- Surface Laptop 6 equivalent (next-gen match)  
- Available 16GB configuration options  
- Suggested upgrade: next-gen 64GB configuration (if applicable)  

---

## Architecture (Conceptual)

- Input layer: SKU or configuration request  
- Logic engine: rule-based matching and mapping  
- Recommendation layer:
  - Equivalent SKUs  
  - Alternative options  
  - Upgrade paths  

Future enhancements may include:
- AI-driven vector search using embeddings  
- Integration with structured product datasets  
- Real-time inventory API connections  

---

## Technologies & Concepts

- Python / Logic-based processing (in progress)  
- Data mapping and transformation  
- AI recommendation system patterns  
- Future:
  - Azure OpenAI  
  - RAG (Retrieval-Augmented Generation)  
  - Copilot-style interaction models  

---

## Future Roadmap

- Integrate FAQ-based knowledge retrieval (RAG architecture)  
- Expand recommendation engine with AI-driven matching  
- Deploy as an interactive assistant (e.g., "Ask RJ")  
- Connect to live inventory and pricing systems  

---

## Why This Matters

This project demonstrates how AI-inspired logic systems can:
- Improve decision-making speed
- Standardize recommendations across teams
- Enhance customer and sales experiences
- Bridge business requirements with technical solution design

---

## Author

Audrey Brigham  
Business Development Manager transitioning into AI & Copilot Solutions Engineering  

``
