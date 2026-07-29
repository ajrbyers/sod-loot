from django.urls import path

from checker import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/eligibility", views.eligibility, name="eligibility"),
    path("api/items", views.item_search, name="item_search"),
]
