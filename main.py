from flask import Flask, render_template, request, jsonify
from urllib.parse import quote_plus
import sqlite3
import requests
import os
import json
from datetime import datetime

app = Flask(__name__)

OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY",      "YOUR_OPENAI_KEY")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS grocery (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT    NOT NULL,
            quantity  REAL    NOT NULL DEFAULT 1,
            unit      TEXT    DEFAULT 'pcs',
            category  TEXT    DEFAULT 'General',
            expiry    DATE    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recipes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT NOT NULL,
            ingredients  TEXT,
            steps        TEXT,
            image_url    TEXT,
            source_url   TEXT,
            saved_at     DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS shopping_list (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT    NOT NULL,
            quantity  REAL    NOT NULL DEFAULT 1,
            unit      TEXT    DEFAULT 'pcs',
            added_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    try:
        conn.execute("ALTER TABLE grocery ADD COLUMN category TEXT DEFAULT 'General'")
        conn.commit()
    except Exception:
        pass  
    conn.commit()
    conn.close()


init_db()


def days_until(date_str: str) -> int:
    expiry = datetime.strptime(date_str, "%Y-%m-%d").date()
    return (expiry - datetime.today().date()).days


def classify_item(item: dict) -> dict:
    """Add days_left and status to a grocery dict."""
    d = days_until(item['expiry'])
    item['days_left'] = d
    if d < 0:
        item['status'] = 'expired'
    elif d <= 2:
        item['status'] = 'critical'
    elif d <= 5:
        item['status'] = 'warning'
    else:
        item['status'] = 'good'
    return item


def get_all_items():
    conn = get_db()
    rows = conn.execute('SELECT * FROM grocery ORDER BY expiry ASC').fetchall()
    conn.close()
    return [classify_item(dict(row)) for row in rows]


# âââââââââââââââââââââââââââââââââââââââââââââ
#  MAIN ROUTE
# âââââââââââââââââââââââââââââââââââââââââââââ
@app.route('/')
def index():
    items = get_all_items()
    alerts        = [i for i in items if i['status'] in ('critical', 'expired')]
    expiring_soon = [i for i in items if i['status'] == 'warning']

    conn = get_db()
    saved_recipes = [dict(r) for r in conn.execute(
        'SELECT * FROM recipes ORDER BY saved_at DESC LIMIT 6'
    ).fetchall()]
    conn.close()

    return render_template(
        'index.html',
        items=items,
        alerts=alerts,
        expiring_soon=expiring_soon,
        saved_recipes=saved_recipes,
        today=datetime.today().strftime("%Y-%m-%d")
    )


@app.route('/add', methods=['POST'])
def add():
    name     = request.form.get('name', '').strip()
    quantity = request.form.get('quantity', 1)
    unit     = request.form.get('unit', 'pcs')
    category = request.form.get('category', 'General').strip() or 'General'
    expiry   = request.form.get('expiry', '')

    if not name or not expiry:
        return jsonify({'error': 'Name and expiry are required'}), 400

    conn = get_db()
    conn.execute(
        'INSERT INTO grocery (name, quantity, unit, category, expiry) VALUES (?, ?, ?, ?, ?)',
        (name, float(quantity), unit, category, expiry)
    )
    conn.commit()
    conn.close()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True})
    from flask import redirect
    return redirect('/')


@app.route('/delete/<int:item_id>', methods=['GET', 'POST', 'DELETE'])
def delete(item_id):
    conn = get_db()
    conn.execute('DELETE FROM grocery WHERE id = ?', (item_id,))
    conn.commit()
    conn.close()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.method == 'DELETE':
        return jsonify({'success': True})
    from flask import redirect
    return redirect('/')


@app.route('/api/update-item/<int:item_id>', methods=['POST'])
def update_item(item_id):
    """Update any fields of a grocery item (AJAX)."""
    data = request.get_json() or {}
    fields = []
    values = []

    if 'quantity' in data:
        fields.append('quantity = ?')
        values.append(float(data['quantity']))
    if 'expiry' in data:
        fields.append('expiry = ?')
        values.append(data['expiry'])
    if 'name' in data:
        fields.append('name = ?')
        values.append(data['name'].strip())
    if 'unit' in data:
        fields.append('unit = ?')
        values.append(data['unit'])
    if 'category' in data:
        fields.append('category = ?')
        values.append(data['category'])

    if not fields:
        return jsonify({'error': 'No fields to update'}), 400

    values.append(item_id)
    conn = get_db()
    conn.execute(f"UPDATE grocery SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()


    row = conn.execute('SELECT * FROM grocery WHERE id = ?', (item_id,)).fetchone()
    conn.close()

    if not row:
        return jsonify({'error': 'Item not found'}), 404

    updated = classify_item(dict(row))
    return jsonify({'success': True, 'item': updated})



@app.route('/api/search-recipes')
def search_recipes():
    query       = request.args.get('q', '').strip()
    ingredients = request.args.get('ingredients', '').strip()

    if not query and not ingredients:
        return jsonify({'error': 'Provide a query or ingredients'}), 400

    try:
        if query:
            resp = requests.get(f'https://www.themealdb.com/api/json/v1/1/search.php?s={query}', timeout=12)
        else:
           
            first_ing = ingredients.split(',')[0].strip()
            resp = requests.get(f'https://www.themealdb.com/api/json/v1/1/filter.php?i={first_ing}', timeout=12)
            
        resp.raise_for_status()
        data = resp.json()
        
        meals = data.get('meals') or []
        recipes = [{
            'id':             m.get('idMeal'),
            'title':          m.get('strMeal'),
            'image':          m.get('strMealThumb'),
            'sourceUrl':      m.get('strSource', ''),
            'readyInMinutes': 30, 
            'servings':       2,  
        } for m in meals[:8]]

        return jsonify({'results': recipes})

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Recipe API timed out. Try again.'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/recipe-detail/<int:recipe_id>')
def recipe_detail(recipe_id):
    try:
        resp = requests.get(f'https://www.themealdb.com/api/json/v1/1/lookup.php?i={recipe_id}', timeout=12)
        resp.raise_for_status()
        data = resp.json()
        
        meals = data.get('meals')
        if not meals:
            return jsonify({'error': 'Recipe not found'}), 404
            
        meal = meals[0]

        ingredients = []
        for i in range(1, 21):
            ing = meal.get(f'strIngredient{i}')
            measure = meal.get(f'strMeasure{i}')
            if ing and ing.strip():
                ingredients.append(f"{measure} {ing}".strip())
                
        steps = [s.strip() for s in meal.get('strInstructions', '').split('\n') if s.strip()]

        return jsonify({
            'id':             meal.get('idMeal'),
            'title':          meal.get('strMeal'),
            'image':          meal.get('strMealThumb'),
            'sourceUrl':      meal.get('strSource', '') or meal.get('strYoutube', ''),
            'ingredients':    ingredients,
            'steps':          steps,
            'readyInMinutes': 30,
            'servings':       2,
        })

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Recipe detail request timed out.'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/shopping-list')
def get_shopping_list():
    conn = get_db()
    rows = [dict(r) for r in conn.execute('SELECT * FROM shopping_list ORDER BY added_at DESC').fetchall()]
    conn.close()
    return jsonify({'items': rows})

@app.route('/api/shopping-list/add', methods=['POST'])
def add_shopping_list():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    quantity = data.get('quantity', 1)
    unit = data.get('unit', 'pcs')
    if not name:
        return jsonify({'error': 'Name required'}), 400
    
    conn = get_db()
    conn.execute('INSERT INTO shopping_list (name, quantity, unit) VALUES (?, ?, ?)', (name, float(quantity), unit))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/shopping-list/delete/<int:item_id>', methods=['DELETE'])
def delete_shopping_list(item_id):
    conn = get_db()
    conn.execute('DELETE FROM shopping_list WHERE id = ?', (item_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/shopping-list/move/<int:item_id>', methods=['POST'])
def move_shopping_list(item_id):
    data = request.get_json() or {}
    expiry = data.get('expiry')
    category = data.get('category', 'General')
    if not expiry:
        return jsonify({'error': 'Expiry date required'}), 400
    
    conn = get_db()
    item = conn.execute('SELECT * FROM shopping_list WHERE id = ?', (item_id,)).fetchone()
    if not item:
        conn.close()
        return jsonify({'error': 'Item not found'}), 404
        

    conn.execute(
        'INSERT INTO grocery (name, quantity, unit, category, expiry) VALUES (?, ?, ?, ?, ?)',
        (item['name'], item['quantity'], item['unit'], category, expiry)
    )
    conn.execute('DELETE FROM shopping_list WHERE id = ?', (item_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/ai-suggestions')
def ai_suggestions():
    items = get_all_items()
    if not items:
        return jsonify({'error': 'No grocery items found. Add some items to your pantry first!'}), 400

    expiring  = [i for i in items if i['days_left'] <= 5]
    all_names = [f"{i['name']} ({i['quantity']} {i['unit']})" for i in items]
    exp_names = [i['name'] for i in expiring]

    prompt = f"""You are a helpful culinary AI assistant for a smart grocery management system.

The user's current pantry contains: {', '.join(all_names)}.
Items expiring within 5 days that should be used first: {', '.join(exp_names) if exp_names else 'None'}.

Please suggest exactly 3 creative, practical recipes using primarily the available pantry items.
Prioritise recipes that use the expiring items.

Return ONLY a valid JSON array with no extra text, markdown, or explanation. Format:

[
  {{
    "title": "Recipe Name",
    "description": "One sentence description of the dish",
    "uses_expiring": true,
    "ingredients_needed": ["1 cup item1", "2 tbsp item2"],
    "quick_steps": ["Step 1 description", "Step 2 description", "Step 3 description"],
    "prep_time": "20 mins",
    "difficulty": "Easy"
  }},
  {{
    "title": "Second Recipe",
    "description": "One sentence description",
    "uses_expiring": false,
    "ingredients_needed": ["item1", "item2"],
    "quick_steps": ["Step 1", "Step 2"],
    "prep_time": "30 mins",
    "difficulty": "Medium"
  }},
  {{
    "title": "Third Recipe",
    "description": "One sentence description",
    "uses_expiring": true,
    "ingredients_needed": ["item1", "item2"],
    "quick_steps": ["Step 1", "Step 2"],
    "prep_time": "15 mins",
    "difficulty": "Easy"
  }}
]
"""

    def get_mock_suggestions():
        return [
            {
                "title": "Pantry Surprise Mix",
                "description": "A delightful and quick mix of your available ingredients.",
                "uses_expiring": bool(exp_names),
                "ingredients_needed": exp_names[:2] if exp_names else all_names[:2],
                "quick_steps": ["Chop ingredients", "Mix well", "Cook and serve"],
                "prep_time": "15 mins",
                "difficulty": "Easy"
            },
            {
                "title": "Chef's Special Pan Roast",
                "description": "A hearty meal making the most of your pantry.",
                "uses_expiring": False,
                "ingredients_needed": all_names[:3],
                "quick_steps": ["Preheat pan", "Roast ingredients evenly", "Garnish and enjoy"],
                "prep_time": "30 mins",
                "difficulty": "Medium"
            }
        ]

    if OPENAI_API_KEY == "YOUR_OPENAI_KEY":
        return jsonify({'suggestions': get_mock_suggestions(), 'expiring_used': exp_names, 'mocked': True})

    try:
        resp = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {OPENAI_API_KEY}',
                'Content-Type':  'application/json',
            },
            json={
                'model': 'gpt-3.5-turbo',
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.7
            },
            timeout=35
        )
        resp.raise_for_status()
        text = resp.json()['choices'][0]['message']['content'].strip()

        start = text.find('[')
        end   = text.rfind(']') + 1
        if start == -1 or end == 0:
            return jsonify({'error': 'AI returned unexpected format. Please try again.'}), 500

        suggestions = json.loads(text[start:end])
        return jsonify({'suggestions': suggestions, 'expiring_used': exp_names})

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else 0
        if status == 401:
            
            return jsonify({'suggestions': get_mock_suggestions(), 'expiring_used': exp_names, 'mocked': True})
        if status == 429:
            return jsonify({'error': 'AI rate limit reached. Please wait a moment.'}), 429
        return jsonify({'error': f'API error {status}: {str(e)}'}), 500
    except requests.exceptions.Timeout:
        return jsonify({'error': 'AI timed out. Please try again.'}), 504
    except json.JSONDecodeError as e:
        return jsonify({'error': f'Failed to parse AI response as JSON: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/api/save-recipe', methods=['POST'])
def save_recipe():
    data = request.get_json()
    if not data or not data.get('title'):
        return jsonify({'error': 'Invalid data: title is required'}), 400

    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO recipes (title, ingredients, steps, image_url, source_url) VALUES (?, ?, ?, ?, ?)',
        (
            data.get('title'),
            data.get('ingredients', ''),
            data.get('steps', ''),
            data.get('image_url', ''),
            data.get('source_url', ''),
        )
    )
    new_id = cursor.lastrowid
    conn.commit()

    row = dict(conn.execute('SELECT * FROM recipes WHERE id = ?', (new_id,)).fetchone())
    conn.close()
    return jsonify({'success': True, 'recipe': row})


@app.route('/api/delete-recipe/<int:recipe_id>', methods=['DELETE'])
def delete_recipe(recipe_id):
    conn = get_db()
    conn.execute('DELETE FROM recipes WHERE id = ?', (recipe_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/saved-recipes')
def get_saved_recipes():
    """Return all saved recipes as JSON for dynamic client-side refresh."""
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        'SELECT * FROM recipes ORDER BY saved_at DESC'
    ).fetchall()]
    conn.close()
    return jsonify({'recipes': rows})
@app.route('/api/pantry')
def pantry_json():
    """Return all pantry items as JSON."""
    return jsonify({'items': get_all_items()})


@app.route('/api/ai-meal-plan')
def ai_meal_plan():
    items = get_all_items()
    if not items:
        return jsonify({'error': 'Add pantry items first!'}), 400

    all_names = [i['name'] for i in items]
    exp_names = [i['name'] for i in items if i['days_left'] <= 5]

    def get_mock_meal_plan():
        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        plan = []
        for idx, day in enumerate(days):
            ing1 = all_names[idx % len(all_names)]
            ing2 = all_names[(idx + 1) % len(all_names)]
            ing3 = all_names[(idx + 2) % len(all_names)]
            plan.append({
                'day': day,
                'breakfast': f'{ing1} Toast & Smoothie',
                'lunch': f'{ing2} Salad Bowl',
                'dinner': f'{ing3} Stir Fry',
                'uses_expiring': ing1 in exp_names or ing2 in exp_names or ing3 in exp_names
            })
        return plan

    if OPENAI_API_KEY == 'YOUR_OPENAI_KEY':
        return jsonify({'plan': get_mock_meal_plan(), 'mocked': True})

    prompt = f"""You are a meal planning AI. The user's pantry has: {', '.join(all_names)}.
Items expiring soon: {', '.join(exp_names) if exp_names else 'None'}.

Create a 7-day meal plan (Monday to Sunday) using these pantry items. Prioritise expiring items.
Return ONLY a valid JSON array, no extra text:
[
  {{
    "day": "Monday",
    "breakfast": "Dish name",
    "lunch": "Dish name",
    "dinner": "Dish name",
    "uses_expiring": true
  }}
]
"""
    try:
        resp = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={'Authorization': f'Bearer {OPENAI_API_KEY}', 'Content-Type': 'application/json'},
            json={'model': 'gpt-3.5-turbo', 'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0.7},
            timeout=35
        )
        resp.raise_for_status()
        text = resp.json()['choices'][0]['message']['content'].strip()
        start, end = text.find('['), text.rfind(']') + 1
        if start == -1 or end == 0:
            return jsonify({'plan': get_mock_meal_plan(), 'mocked': True})
        return jsonify({'plan': json.loads(text[start:end])})
    except Exception:
        return jsonify({'plan': get_mock_meal_plan(), 'mocked': True})

 
@app.route('/api/ai-nutrition')
def ai_nutrition():
    items = get_all_items()
    if not items:
        return jsonify({'error': 'Add pantry items first!'}), 400

    all_names = [i['name'] for i in items]

    protein_words = ['chicken', 'egg', 'fish', 'meat', 'paneer', 'tofu', 'dal', 'lentil', 'bean', 'milk', 'curd', 'yogurt', 'cheese']
    veggie_words  = ['tomato', 'onion', 'potato', 'carrot', 'spinach', 'broccoli', 'pepper', 'cabbage', 'cucumber', 'lettuce', 'peas', 'corn', 'mushroom']
    fruit_words   = ['apple', 'banana', 'orange', 'mango', 'grape', 'berry', 'lemon', 'watermelon', 'papaya', 'pineapple']
    grain_words   = ['rice', 'wheat', 'bread', 'pasta', 'oats', 'flour', 'noodle', 'cereal', 'roti', 'chapati']
    dairy_words   = ['milk', 'curd', 'yogurt', 'cheese', 'butter', 'cream', 'paneer', 'ghee']

    def check_category(names, keywords):
        return [n for n in names if any(k in n.lower() for k in keywords)]

    proteins = check_category(all_names, protein_words)
    veggies  = check_category(all_names, veggie_words)
    fruits   = check_category(all_names, fruit_words)
    grains   = check_category(all_names, grain_words)
    dairy    = check_category(all_names, dairy_words)

    categories = {'proteins': proteins, 'vegetables': veggies, 'fruits': fruits, 'grains': grains, 'dairy': dairy}
    filled = sum(1 for v in categories.values() if v)
    score = int((filled / 5) * 100)

    missing = []
    if not proteins: missing.append('ð¥© Protein (chicken, eggs, lentils, tofu)')
    if not veggies:  missing.append('ð¥¦ Vegetables (spinach, tomatoes, carrots)')
    if not fruits:   missing.append('ð Fruits (apples, bananas, oranges)')
    if not grains:   missing.append('ð¾ Grains (rice, bread, oats)')
    if not dairy:    missing.append('ð¥ Dairy (milk, yogurt, cheese)')

    tips = []
    if score == 100:
        tips.append('ð Excellent! Your pantry covers all major food groups.')
    elif score >= 60:
        tips.append('ð Good balance! Consider adding items from the missing groups.')
    else:
        tips.append('â ï¸ Your pantry needs more variety for a balanced diet.')

    return jsonify({
        'score': score,
        'categories': {k: v for k, v in categories.items()},
        'missing': missing,
        'tips': tips,
        'total_items': len(all_names)
    })


@app.route('/api/export-shopping-list')
def export_shopping_list():
    conn = get_db()
    rows = conn.execute('SELECT * FROM shopping_list ORDER BY added_at DESC').fetchall()
    conn.close()
    lines = ['Item,Quantity,Unit']
    for r in rows:
        lines.append(f"{r['name']},{r['quantity']},{r['unit']}")
    from flask import Response
    return Response('\n'.join(lines), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=shopping_list.csv'})


@app.route('/api/export-pantry')
def export_pantry():
    items = get_all_items()
    lines = ['Item,Quantity,Unit,Expiry,Days Left,Status']
    for i in items:
        lines.append(f"{i['name']},{i['quantity']},{i['unit']},{i['expiry']},{i['days_left']},{i['status']}")
    from flask import Response
    return Response('\n'.join(lines), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=pantry_export.csv'})


@app.route('/api/shopping-list/add-missing', methods=['POST'])
def add_missing_ingredients():
    data = request.get_json() or {}
    ingredients = data.get('ingredients', [])
    if not ingredients:
        return jsonify({'error': 'No ingredients provided'}), 400

    pantry_names = [i['name'].lower() for i in get_all_items()]
    conn = get_db()
    added = []
    for ing in ingredients:
        name = ing.strip()
        
        if not any(pn in name.lower() or name.lower() in pn for pn in pantry_names):
            conn.execute('INSERT INTO shopping_list (name, quantity, unit) VALUES (?, ?, ?)', (name, 1, 'pcs'))
            added.append(name)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'added': added, 'skipped': len(ingredients) - len(added)})

QUICK_COMMERCE_PLATFORMS = [
    {
        'id':    'blinkit',
        'name':  'Blinkit',
        'color': '#f8cb46',
        'text_color': '#1a1a1a',
        'icon':  'â¡',
        'url':   'https://blinkit.com/s/?q={query}',
        'tagline': '10-min delivery',
    },
    {
        'id':    'instamart',
        'name':  'Swiggy Instamart',
        'color': '#fc8019',
        'text_color': '#ffffff',
        'icon':  'ðµ',
        'url':   'https://www.swiggy.com/instamart/search?custom_back=true&query={query}',
        'tagline': '15-30 min delivery',
    },
    {
        'id':    'bigbasket',
        'name':  'BigBasket',
        'color': '#84c225',
        'text_color': '#ffffff',
        'icon':  'ð',
        'url':   'https://www.bigbasket.com/ps/?q={query}',
        'tagline': 'Scheduled delivery',
    },
    {
        'id':    'zepto',
        'name':  'Zepto',
        'color': '#8025e2',
        'text_color': '#ffffff',
        'icon':  'ð',
        'url':   'https://www.zeptonow.com/search?query={query}',
        'tagline': '10-min delivery',
    },
]


@app.route('/api/quick-commerce/platforms')
def quick_commerce_platforms():
    """Return the list of supported quick-commerce platforms."""
    return jsonify({'platforms': QUICK_COMMERCE_PLATFORMS})


@app.route('/api/quick-commerce/search-links')
def quick_commerce_links():
    """Generate search URLs for a product across all platforms."""
    product = request.args.get('product', '').strip()
    if not product:
        return jsonify({'error': 'Product name is required'}), 400

    encoded = quote_plus(product)
    links = []
    for p in QUICK_COMMERCE_PLATFORMS:
        links.append({
            **p,
            'search_url': p['url'].replace('{query}', encoded),
        })
    return jsonify({'product': product, 'links': links})


@app.route('/api/quick-commerce/search-all-links')
def quick_commerce_all_links():
    """Generate search URLs for ALL shopping list items across all platforms."""
    conn = get_db()
    rows = [dict(r) for r in conn.execute('SELECT * FROM shopping_list ORDER BY added_at DESC').fetchall()]
    conn.close()

    results = []
    for item in rows:
        encoded = quote_plus(item['name'])
        item_links = []
        for p in QUICK_COMMERCE_PLATFORMS:
            item_links.append({
                **p,
                'search_url': p['url'].replace('{query}', encoded),
            })
        results.append({
            'item': item,
            'links': item_links,
        })
    return jsonify({'results': results})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
