from django.forms.models import modelformset_factory
from django import forms
from django.db import models
from config.models import Bulb

class BulbForm(forms.ModelForm):
    note = ""

    class Meta:
        model = Bulb
        fields = '__all__'

    def save(self, commit=True):
        return super(BulbForm, self).save(commit=commit)

BulbFormSet = modelformset_factory(Bulb, form=BulbForm, exclude=(), extra=0, can_delete=True)