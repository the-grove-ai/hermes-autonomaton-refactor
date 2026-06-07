---
name: markdown-to-notion
description: Intelligently saves markdown files and content to Notion with automatic chunking for large documents, category routing to the thought capture database (b5eb61c0-af3f-4a43-9b5d-6e28f97377b8), and support for both mobile and desktop workflows. Use when the user asks to save, log, capture, or store markdown content, documents, notes, or thoughts to Notion. Also triggers on phrases like "remember this", "save to Notion", "capture thought", or "log this".
---

# Markdown to Notion

Saves markdown files and content directly to your Notion thought capture database with intelligent chunking, categorization, and linking. Works seamlessly from Claude mobile, web, or desktop.

## Quick Start

**Single command capture:**
```
User: "Save to Notion: Download Claude design skills blog"
Claude: ✅ Captured to Notion (Category: References)
```

**Markdown file capture:**
```
User: "Log this markdown file to Notion"
Claude: [analyzes file] 
- Size: 450 blocks ✓
- Creates page with proper title
- Routes to correct category
✅ Saved: Chase Partnership Strategy Memo
```

**Large document handling:**
```
User: "Save this 50-page financial model to Notion"
Claude: ⚠️ Document is 2,800 blocks (limit: 2,000)
Creating parent + child pages:
  ✅ Parent: Financial Model Overview
  ✅ Child: Revenue Projections  
  ✅ Child: Cost Structure
  ✅ Child: Scenario Analysis
All pages linked in: Client - Walmart
```

## Core Workflow

### Step 1: Analyze Content
```
Determine:
- Content type (note, document, strategy, memo, insight)
- Size in blocks (paragraphs + headings + lists)
- Appropriate category for routing
- Urgency flag if mentioned
```

### Step 2: Size Check
```
Count blocks (paragraph = 1, heading = 1, list item = 1):
- < 1,500 blocks → Single page strategy
- 1,500-5,000 blocks → Parent + children by H1 sections
- > 5,000 blocks → Warn user, suggest summarization

Notion limit: 2,000 blocks per page
Safety margin: Use 1,500 as single-page threshold
```

### Step 3: Route to Category
```
Analyze content and user context to determine category:

Category Map (Notion "Thought Category" property):
- "Product Development" → product features, roadmaps, specs
- "Business Strategy" → partnerships, growth, revenue
- "Technical Documentation" → code, architecture, systems
- "Design System" → UI/UX, brand, visual
- "Client - Chase" → Chase partnership work
- "Client - Walmart" → Walmart/DrumWave work  
- "Action Items" → todos, tasks, follow-ups
- "Ventures" → startup ideas, investments
- "Knowledge Base" → insights, learnings, observations
- "References" → articles, resources, links

Default: "Knowledge Base" if unclear
```

### Step 4: Create in Notion
```
Single Page:
- Use mcp_notion_notion_create_pages
- Parent: data_source_id "b5eb61c0-af3f-4a43-9b5d-6e28f97377b8"
- Properties: Title, Category, Urgent flag, CaptureDate
- Content: Full markdown

Multi-Page (chunked):
- Create parent page with overview
- Create child pages by H1 sections
- Link children to parent
- All use same category
```

## Category Intelligence

### Auto-Detection Patterns
```
Content mentions → Category
"Chase", "JPM", "credit card partnership" → Client - Chase
"Walmart", "DrumWave", "retail media" → Client - Walmart
"feature", "roadmap", "product spec" → Product Development
"bug fix", "technical debt", "architecture" → Technical Documentation
"todo", "action item", "follow up" → Action Items
"insight", "learning", "observation" → Knowledge Base
"article", "resource", "link" → References
```

### Context Clues
```
Check recent conversation for:
- Project names (DrumWave → Client - Walmart)
- Client mentions (Chase meeting → Client - Chase)
- Work type (strategy doc → Business Strategy)
- Urgency markers (ASAP, urgent → set flag)
```

## Size Management

### Block Counting Logic
```python
def estimate_blocks(markdown_content):
    """Estimate Notion blocks from markdown"""
    lines = markdown_content.split('\n')
    blocks = 0
    
    for line in lines:
        if not line.strip():
            continue  # Empty lines don't create blocks
        
        # Headings = 1 block each
        if line.startswith('#'):
            blocks += 1
        # List items = 1 block each
        elif line.strip().startswith(('-', '*', '1.', '2.')):
            blocks += 1
        # Paragraphs = 1 block each
        elif line.strip():
            blocks += 1
    
    return blocks
```

### Chunking Strategy
```
For documents > 1,500 blocks:

1. Split by H1 headings (# Section)
2. Create parent page with:
   - Document title
   - Executive summary (first 2-3 paragraphs)
   - Table of contents linking to children
3. Create child pages for each H1 section
4. Ensure all pages share same category
5. Link children back to parent

Example structure:
Parent: "DrumWave Financial Model" (200 blocks)
  ├─ Child: "Consumer Earnings Module" (800 blocks)
  ├─ Child: "Network Effects Calculator" (600 blocks)
  └─ Child: "ROI Projections" (900 blocks)
```

## Notion Database Schema

**Database ID**: `b5eb61c0-af3f-4a43-9b5d-6e28f97377b8`

**Properties**:
- **Name** (Title): Thought summary or document title
- **Thought Category** (Select): Classification per category map above
- **Content** (Rich Text): Full markdown content
- **Urgent** (Checkbox): Priority flag
- **ThoughtID** (Text): Unique identifier (auto-generated timestamp)
- **CaptureDate** (Date): ISO 8601 timestamp
- **Tags** (Multi-select): Auto-extracted keywords
- **SyncStatus** (Select): "Synced" (always for this skill)
- **SourceFile** (Text): Original file path if applicable

## Natural Language Triggers

When user says these phrases, capture immediately:

| User Says | Action |
|-----------|--------|
| "Save to Notion: [content]" | Create page in appropriate category |
| "Log this markdown" | Save current file to Notion |
| "Capture thought: [text]" | Quick capture to Knowledge Base |
| "Remember: [text]" | Quick capture with auto-category |
| "Todo: [task]" | Capture to Action Items, set urgent |
| "URGENT: [text]" | Capture with urgent flag set |
| "Save this doc to Notion" | Analyze and save with chunking if needed |

## Mobile Optimization

**Design for mobile usage:**
1. Keep confirmations brief (✅ Captured)
2. Don't ask for permission - just execute
3. Show category and location clearly
4. Provide Notion link in response
5. Handle voice input gracefully

**Mobile-friendly responses:**
```
Good: ✅ Captured to Business Strategy
Bad: "I've successfully saved your thought to the Notion database under the Business Strategy category. You can view it at..."
```

## Error Handling

| Issue | Solution |
|-------|----------|
| Database not found | Search for database ID, inform user if missing |
| Content too large | Chunk automatically, explain approach |
| Category unclear | Default to Knowledge Base, notify user |
| Notion API error | Retry once, then inform user to check Notion |
| Empty content | Ask user to provide content first |

## Best Practices

1. **Capture immediately** when triggered - don't ask permission
2. **Infer category** from context rather than asking user
3. **Chunk proactively** for documents > 1,500 blocks
4. **Link intelligently** - connect related pages
5. **Extract title** from first H1 or first sentence
6. **Set urgency** when user mentions time pressure
7. **Keep responses concise** especially for mobile
8. **Provide Notion link** so user can view/edit

## Examples

See [references/capture-examples.md](references/capture-examples.md) for complete workflows showing:
- Quick thought capture
- Strategy memo save
- Large document chunking
- Multi-project categorization
- Mobile vs desktop patterns
