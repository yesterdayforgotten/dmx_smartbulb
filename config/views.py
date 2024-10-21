from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from config.models import Bulb
from config.forms import BulbFormSet
from .kasabulb.kasabulb import Kasa
from django.forms.models import modelformset_factory
from .forms import BulbForm
import json

# Create your views here.

def bulb_query(request, bulb_id):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if is_ajax:
        bulb = get_object_or_404(Bulb, id=bulb_id)
        data = Kasa.get_state(bulb.ip_addr)

        return JsonResponse({'status': 'success', 'hue': data['system']['get_sysinfo']['light_state']['hue'], 'sat': data['system']['get_sysinfo']['light_state']['saturation'], 'val': data['system']['get_sysinfo']['light_state']['brightness']})
    else:
        return HttpResponseBadRequest('Invalid request')
    
def bulb_set(request, bulb_id, h, s, v):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if is_ajax:
        bulb = get_object_or_404(Bulb, id=bulb_id)
        Kasa.change_color(bulb.ip_addr, h, s, v)

        return JsonResponse({'status': 'success'})
    else:
        return HttpResponseBadRequest('Invalid request')

def home(request):
    if request.method == "GET" and request.GET.get('discovery') != None:
        kasa_devices = Kasa.discovery().items()
        found = []
        new = []

        for ip, info in kasa_devices:
            match = False
            for bulb in Bulb.objects.all():
                if str(bulb.ip_addr) == ip:
                    found.append(ip)
                    match = True
                    break
            if match == False:
                new.append({'name': info['system']['get_sysinfo']['alias'],'ip_addr':ip,'channel':0})
        
        ff = modelformset_factory(Bulb, form=BulbForm, exclude=(), extra=len(new), can_delete=True)
        formset = ff(prefix="bulbs",initial=new)

        for form in formset:
            if str(form.instance.ip_addr) in found:
                form.note = "found"
            if form.instance.id == None:
                form.note = "new"
        
    elif request.method == "POST":
        formset = BulbFormSet(data=request.POST, prefix="bulbs")
        if formset.is_valid():
            formset.save()
            formset = BulbFormSet(prefix="bulbs")
    else:
        formset = BulbFormSet(prefix="bulbs")
        
    return render(request, "pages/home.html", {"formset": formset})

