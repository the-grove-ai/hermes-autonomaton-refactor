# Capture Examples

Complete workflows showing different capture scenarios.

## Example 1: Quick Thought Capture (Mobile)

**User**: "Save to Notion: Download Claude design skills from https://www.claude.com/blog/improving-frontend-design-through-skills"

**Claude Action**:
```
1. Detect trigger: "Save to Notion"
2. Extract content: URL + description
3. Analyze category: "design skills" + "blog" → References
4. Estimate size: ~50 characters → Single thought
5. Set urgency: No markers → Not urgent
```

**Notion API Call**:
```json
{
  "parent": {
    "data_source_id": "b5eb61c0-af3f-4a43-9b5d-6e28f97377b8"
  },
  "pages": [{
    "properties": {
      "Name": "Download Claude design skills from blog",
      "Thought Category": "References",
      "Urgent": "__NO__",
      "ThoughtID": "2025-11-14T10:30:00Z",
      "date:CaptureDate:start": "2025-11-14",
      "date:CaptureDate:is_datetime": 1
    },
    "content": "Download Claude design skills from https://www.claude.com/blog/improving-frontend-design-through-skills"
  }]
}
```

**Response**: 
```
✅ Captured to References
```

---

## Example 2: Strategy Memo (Medium Document)

**User**: "Log this markdown to Notion" [referring to 400-block strategy doc]

**Claude Action**:
```
1. Read file content
2. Estimate blocks: 400 (within single-page limit)
3. Extract title from first H1: "Chase Partnership Q1 Strategy"
4. Detect category: "Chase" mentioned → Client - Chase
5. Check urgency: No markers → Not urgent
```

**Notion API Call**:
```json
{
  "parent": {
    "data_source_id": "b5eb61c0-af3f-4a43-9b5d-6e28f97377b8"
  },
  "pages": [{
    "properties": {
      "Name": "Chase Partnership Q1 Strategy",
      "Thought Category": "Client - Chase",
      "Urgent": "__NO__",
      "ThoughtID": "2025-11-14T10:35:00Z",
      "date:CaptureDate:start": "2025-11-14",
      "date:CaptureDate:is_datetime": 1,
      "SourceFile": "chase-q1-strategy.md"
    },
    "content": "[Full markdown content here - 400 blocks]"
  }]
}
```

**Response**:
```
✅ Saved: Chase Partnership Q1 Strategy
Category: Client - Chase
Blocks: 400
```

---

## Example 3: Large Document Chunking

**User**: "Save this financial model doc to Notion" [2,800 block document]

**Claude Action**:
```
1. Read file content
2. Estimate blocks: 2,800 (exceeds 2,000 limit)
3. Identify H1 sections:
   - Revenue Projections (800 blocks)
   - Cost Structure (600 blocks)
   - Network Effects (700 blocks)
   - Scenario Analysis (700 blocks)
4. Create chunking strategy: Parent + 4 children
5. Extract category: "DrumWave" mentioned → Client - Walmart
```

**Parent Page API Call**:
```json
{
  "parent": {
    "data_source_id": "b5eb61c0-af3f-4a43-9b5d-6e28f97377b8"
  },
  "pages": [{
    "properties": {
      "Name": "DrumWave Financial Model Documentation",
      "Thought Category": "Client - Walmart",
      "Urgent": "__NO__",
      "ThoughtID": "2025-11-14T10:40:00Z-parent",
      "date:CaptureDate:start": "2025-11-14",
      "date:CaptureDate:is_datetime": 1
    },
    "content": "# DrumWave Financial Model Documentation\n\n## Overview\n[First 2-3 paragraphs]\n\n## Sections\n- <mention-page url='...'>Revenue Projections</mention-page>\n- <mention-page url='...'>Cost Structure</mention-page>\n- <mention-page url='...'>Network Effects</mention-page>\n- <mention-page url='...'>Scenario Analysis</mention-page>"
  }]
}
```

**Child Pages API Calls** (4 separate calls):
```json
{
  "parent": {
    "page_id": "[parent-page-id]"
  },
  "pages": [{
    "properties": {
      "Name": "Revenue Projections",
      "Thought Category": "Client - Walmart",
      "ThoughtID": "2025-11-14T10:40:00Z-child-1"
    },
    "content": "[Revenue Projections section content - 800 blocks]"
  }]
}
// Repeat for other 3 sections
```

**Response**:
```
⚠️ Document is 2,800 blocks (limit: 2,000)
Creating parent + child pages:
  ✅ Parent: DrumWave Financial Model Documentation
  ✅ Child: Revenue Projections (800 blocks)
  ✅ Child: Cost Structure (600 blocks)
  ✅ Child: Network Effects (700 blocks)
  ✅ Child: Scenario Analysis (700 blocks)

All pages linked in: Client - Walmart
```

---

## Example 4: Urgent Todo

**User**: "URGENT todo: Fix DrumWave calculator network effects bug before Walmart pitch Friday"

**Claude Action**:
```
1. Detect trigger: "URGENT todo"
2. Extract content: Task description
3. Category: "todo" → Action Items
4. Urgency: "URGENT" keyword → Set urgent flag
5. Extract deadline context: "before Walmart pitch Friday"
```

**Notion API Call**:
```json
{
  "parent": {
    "data_source_id": "b5eb61c0-af3f-4a43-9b5d-6e28f97377b8"
  },
  "pages": [{
    "properties": {
      "Name": "Fix DrumWave calculator network effects bug before Walmart pitch Friday",
      "Thought Category": "Action Items",
      "Urgent": "__YES__",
      "ThoughtID": "2025-11-14T10:45:00Z",
      "date:CaptureDate:start": "2025-11-14",
      "date:CaptureDate:is_datetime": 1
    },
    "content": "Fix DrumWave calculator network effects bug before Walmart pitch Friday"
  }]
}
```

**Response**:
```
✅ Urgent todo captured
Category: Action Items
🚨 Flagged as urgent
```

---

## Example 5: Context-Based Categorization

**Conversation Context**:
```
User: "We need to think through the Chase co-brand credit card economics"
Claude: [discussion about partnership structure]
User: "Save this to Notion"
```

**Claude Action**:
```
1. Detect trigger: "Save this to Notion"
2. Extract content: Recent conversation about Chase partnership
3. Category inference:
   - Mentioned "Chase" → Client - Chase
   - Discussion type: "partnership economics" → Business Strategy
   - Choose: Client - Chase (client takes precedence)
4. Create summary from conversation
```

**Response**:
```
✅ Captured conversation summary
Category: Client - Chase
Topic: Co-brand credit card economics
```

---

## Mobile vs Desktop Patterns

### Mobile (Voice Input)
```
User: [via voice] "Remember, contact Sarah at Chase about partnership metrics"

Claude detects:
- Informal phrasing ("Remember")
- Action item ("contact Sarah")
- Client context ("Chase")

Response: Brief confirmation
✅ Todo saved - Client: Chase
```

### Desktop (Typed)
```
User: Save this 15-page strategy memo to Notion [uploads file]

Claude detects:
- File upload
- Descriptive context ("strategy memo")
- Size estimation needed

Response: Detailed confirmation
✅ Saved: Partnership Growth Strategy Q1-2025
Category: Business Strategy
Size: 1,200 blocks (single page)
[Link to Notion page]
```
