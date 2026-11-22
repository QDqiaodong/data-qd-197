from .checks import fields,array,ident,unique,number

def parse(data,extra=()):
    fields(data,('ingredients','recipes','orders')+tuple(extra))
    ingredients={};recipes={};orders=[]
    for item in array(data['ingredients'],1,100):
        fields(item,('id','yield','price_per_gross_gram','tags'))
        key=ident(item['id'])
        if key in ingredients:raise ValueError('duplicate ingredient')
        y=number(item['yield']);price=number(item['price_per_gross_gram'])
        if not 0<y<=1 or price<0:raise ValueError('invalid yield or price')
        tags=[ident(t) for t in array(item['tags'],0,30)];unique(tags)
        ingredients[key]={'yield':y,'price':price,'tags':tags}
    for item in array(data['recipes'],1,100):
        fields(item,('id','output_grams','components'));key=ident(item['id'])
        if key in recipes:raise ValueError('duplicate recipe')
        output=number(item['output_grams'])
        if output<=0:raise ValueError('output must be positive')
        components=[]
        for c in array(item['components'],1,30):
            fields(c,('kind','ref','net_grams'))
            if c['kind'] not in ('ingredient','recipe'):raise ValueError('invalid component kind')
            grams=number(c['net_grams'])
            if grams<=0:raise ValueError('component must be positive')
            components.append((c['kind'],ident(c['ref']),grams))
        if output>sum(c[2] for c in components):raise ValueError('output exceeds component mass')
        recipes[key]={'output':output,'components':components}
    for key,r in recipes.items():
        for kind,ref,_ in r['components']:
            if ref not in (ingredients if kind=='ingredient' else recipes):raise ValueError('unknown component')
    for item in array(data['orders'],1,100):
        fields(item,('id','recipe','output_grams'));key=ident(item['id']);ref=ident(item['recipe']);grams=number(item['output_grams'])
        if ref not in recipes or grams<=0:raise ValueError('invalid order')
        orders.append((key,ref,grams))
    unique([o[0] for o in orders])
    # Validate all recipes, including ones unused by this order sheet.
    done=set();active=set()
    def visit(key):
        if key in active:raise ValueError('recipe cycle')
        if key in done:return
        active.add(key)
        for kind,ref,_ in recipes[key]['components']:
            if kind=='recipe':visit(ref)
        active.remove(key);done.add(key)
    for key in recipes:visit(key)
    return ingredients,recipes,orders
